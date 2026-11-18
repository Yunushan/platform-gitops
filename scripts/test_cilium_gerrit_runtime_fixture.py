#!/usr/bin/env python3
"""Real Gerrit/Tekton/Argo CD flow and its original workload behind Cilium TLS."""

import base64
import copy
import json
import ssl
from http.client import HTTPException

import deploy_cilium_gateway as routes
import deploy_cilium_installation as installation
import render_cilium_gateway as gateway
from test_argocd_worker_workload import BODY
from test_cilium_worker_runtime_fixture import DirectTLS, IMAGES, certificates, diagnostics, package, stage, verify_datapath
from test_gerrit_worker_runtime_fixture import exercise as application
from test_tekton_worker_runtime_fixture import exercise as pipeline


HOST = "application.example.invalid"
NAMESPACE = "synthetic-application"


def exercise(test, context):
    report = verify_datapath(test, context)
    report.update(pipeline(test, context, workload=lambda actual: application(test, actual, native_watch=True, argocd_check=True)))
    report.update(application_gateway(test, context, report))
    chosen = context["cilium_runtime"]
    checked = installation.operate(chosen["bundle"], chosen["digest"], verify=True, output=chosen["evidence"] / "after-cicd")
    test.assertTrue(checked["bootstrap_ready"])
    report["real_cilium_gerrit_tekton_argocd_flow_verified"] = True
    return report


def application_gateway(test, context, report):
    required = ("real_worker_ci_to_argocd_serving_handoff_verified", "actual_argocd_reconciliation_verified",
        "actual_serving_workload_http_verified", "native_watch_services_started", "native_promoter_cli_verified")
    if any(report.get(key) is not True for key in required): raise ValueError("Require the completed actual worker/native CI/CD flow")
    client, wait = context["client"], context["wait"]
    chosen = context["cilium_runtime"]; plan = chosen["plan"]
    stage("original-argocd-workload-gateway")
    def create(obj): return json.loads(client.call(["create", "-f", "-", "-o", "json"], payload=json.dumps(obj).encode()))
    namespace = client.get("namespace/" + NAMESPACE)
    deployment = client.get("deployment/synthetic-web", NAMESPACE)
    service = client.get("service/synthetic-web", NAMESPACE)
    test.assertEqual(deployment["metadata"]["uid"], report["application_deployment_uid"])
    test.assertEqual(service["metadata"]["uid"], report["application_service_uid"])
    test.assertEqual(service["spec"]["ports"][0]["targetPort"], "http")
    def pods(): return json.loads(client.call(["get", "pods", "-n", NAMESPACE, "-l", "app=synthetic-web", "-o", "json"]))["items"]
    original_pods = {p["metadata"]["uid"]: copy.deepcopy(p) for p in pods()}
    test.assertEqual(set(original_pods), set(report["application_pod_uids"]))
    test.assertEqual(len(original_pods), 2)
    def unchanged():
        test.assertEqual(client.get("namespace/" + NAMESPACE)["metadata"]["uid"], namespace["metadata"]["uid"])
        for kind, original in (("deployment", deployment), ("service", service)):
            current = client.get(kind + "/synthetic-web", NAMESPACE)
            test.assertEqual(current["metadata"]["uid"], original["metadata"]["uid"])
            test.assertEqual(current["spec"], original["spec"])
        current = pods(); test.assertEqual({p["metadata"]["uid"] for p in current}, set(original_pods))
        for pod in current:
            original = original_pods[pod["metadata"]["uid"]]
            test.assertEqual(pod["spec"], original["spec"])
            test.assertTrue(routes.ready_pod(pod))
            test.assertEqual([s["containerID"] for s in pod["status"]["containerStatuses"]],
                [s["containerID"] for s in original["status"]["containerStatuses"]])
        return True
    urls = ["http://" + service["spec"]["clusterIP"] + ":8080/",
        *["http://" + p["status"]["podIP"] + ":8080/" for p in original_pods.values()]]
    command = 'wget -T 2 -qO- "$1"; code=$?; printf "\\nprobe-exit:%s\\n" "$code"'
    def request(index, url): return client.call(["exec", "pod/denied-" + str(index), "-n", "cilium-probe", "--", "sh", "-c", command, "probe", url], seconds=10)
    def direct_access(): return all(request(index, url) == BODY.decode() + "\nprobe-exit:0\n" for index in range(2) for url in urls)
    wait(direct_access, seconds=60)
    owner, gateway_namespace = "fixture-application-edge", "cilium-application-gateway"
    test.assertNotIn(gateway.ACCESS, namespace["metadata"].get("labels", {}))
    patch = [{"op": "test", "path": "/metadata/uid", "value": namespace["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": namespace["metadata"]["resourceVersion"]},
        {"op": "add", "path": "/metadata/labels/" + gateway.ACCESS.replace("~", "~0").replace("/", "~1"), "value": owner}]
    client.call(["patch", "namespace/" + NAMESPACE, "--type=json", "-p", json.dumps(patch)])
    create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": gateway_namespace}})
    pki_root = context["root"] / "application-gateway-pki"; pki_root.mkdir(mode=0o700)
    pki = certificates({**context, "root": pki_root}, hostname=HOST)
    create({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "application-tls", "namespace": gateway_namespace}, "type": "kubernetes.io/tls",
        "data": {"tls.crt": base64.b64encode((pki / "server.crt").read_bytes()).decode(), "tls.key": base64.b64encode((pki / "server.key").read_bytes()).decode()}})
    route_plan = {"schema_version": 1, "name": owner, "namespace": gateway_namespace, "class_name": "fixture-application", "management_target": context["target"],
        "cilium": {"version": "1.19.10", "gateway_api_version": "1.4.1", "namespace": "kube-system", "images": IMAGES},
        "serving_selector": plan["serving_selector"], "failure_domain_key": plan["failure_domain_key"], "https_port": 8444, "tls_secret": "application-tls",
        "routes": [{"name": "application", "namespace": NAMESPACE, "hostname": HOST, "protocol": "http", "service": "synthetic-web", "port": 8080,
            "target_port": 8080, "pod_selector": {"app": "synthetic-web"}}]}
    files = gateway.render(route_plan, "k3s"); bundle = package(context["root"], "application-gateway-package", files)
    digest = json.loads(files["render-status.json"])["contract_sha256"]
    installed = routes.operate(bundle, digest, apply=True, output=chosen["evidence"] / "application-gateway-install")
    test.assertTrue(installed["gateway_programmed"])
    tls = ssl.create_default_context(cafile=str(pki / "ca.crt")); outcomes = []
    def serving():
        outcomes.clear()
        for node in context["nodes"][1:]:
            row = {"node": node["name"], "matched": False}
            connection = DirectTLS(HOST, 8444, context=tls, timeout=3); connection.address = node["address"]
            try:
                connection.request("GET", "/"); response = connection.getresponse(); body = response.read(1024)
                row.update(http_status=response.status, matched=response.status == 200 and body == BODY, tls_verified=True)
            except (OSError, HTTPException) as error:
                row.update(error=type(error).__name__, certificate_verify_code=getattr(error, "verify_code", None))
            finally: connection.close()
            outcomes.append(row)
        return all(row["matched"] for row in outcomes)
    def denied():
        return unchanged() and serving() and all(request(index, url) == "\nprobe-exit:1\n" for index in range(2) for url in urls) and serving()
    try:
        wait(serving, seconds=60); wait(denied, seconds=90)
        checked = routes.operate(bundle, digest, verify=True, output=chosen["evidence"] / "application-gateway-verify")
        test.assertTrue(checked["gateway_programmed"]); test.assertTrue(unchanged())
    except Exception:
        print(json.dumps({"application_gateway_tls_outcomes": outcomes}), flush=True); diagnostics(client); raise
    stage("original-argocd-application-tls-and-policy-verified")
    result = {"application_gateway_tls_serving_verified": True, "application_gateway_policy_denials_verified": True,
        "application_gateway_named_target_port_verified": True, "application_gateway_original_pod_uids": sorted(original_pods),
        "application_gateway_serving_nodes": 2, "application_gateway_physical_ha_verified": False, "full_stack_verified": False}
    if "native_edge_hosts" in context:
        from test_cilium_edge_runtime_fixture import verify
        result.update(verify(test, context, report, bundle, digest, pki, HOST, unchanged, denied))
    return result
