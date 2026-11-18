#!/usr/bin/env python3
"""Check gateway scope, topology, ownership, policy and current-generation evidence."""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import yaml

import deploy_cilium_gateway as deploy
import render_cilium_gateway as renderer
import kubernetes_management as management
from test_tekton import declared_fields


def fixture():
    return {"schema_version": 1, "name": "platform-edge", "namespace": "platform-gateway", "class_name": "platform-cilium",
        "management_target": {"kubeconfig_env": "PLATFORM_MANAGEMENT_KUBECONFIG", "context": "management", "cluster_uid": "11111111-1111-4111-8111-111111111111"},
        "cilium": {"version": "1.18.3", "gateway_api_version": "1.2.0", "namespace": "kube-system",
            "images": {role: "registry.example.com/cilium/" + role + ":v1.18.3@sha256:" + char * 64 for role, char in (("agent", "a"), ("envoy", "b"), ("operator", "c"))}},
        "serving_selector": {"platform.gitops/edge": "true"}, "failure_domain_key": "topology.kubernetes.io/zone",
        "https_port": 8443, "tls_secret": "platform-edge-tls",
        "routes": [{"name": "forge", "namespace": "forgejo", "hostname": "forge.example.com", "protocol": "http", "service": "forgejo-http", "port": 3000,
                    "target_port": 3000, "pod_selector": {"app.kubernetes.io/name": "forgejo"}},
                   {"name": "deployment-rpc", "namespace": "argocd", "hostname": "rpc.example.com", "protocol": "h2c", "service": "argocd-server", "port": 80,
                    "target_port": 8080, "pod_selector": {"app.kubernetes.io/name": "argocd-server"}}]}


def conditions(*names, generation=1):
    return [{"type": name, "status": "True", "observedGeneration": generation} for name in names]


def gerrit_route():
    return {"name": "review", "namespace": "gerrit", "hostname": "review.example.invalid", "protocol": "tls",
            "service": "gerrit-router", "port": 443, "target_port": 8443,
            "pod_selector": {"app.kubernetes.io/name": "gerrit-router"}}


def owner(kind, uid):
    return [{"controller": True, "kind": kind, "uid": uid}]


def pod(role, node, image, kind, uid):
    return {"metadata": {"name": role + "-" + node, "ownerReferences": owner(kind, uid)},
        "spec": {"nodeName": node, "containers": [{"name": role, "image": image}]},
        "status": {"phase": "Running", "conditions": conditions("Ready"),
                   "containerStatuses": [{"name": role, "ready": True, "state": {"running": {}}}]}}


class FakeClient:
    def __init__(self, plan):
        self.plan, self.calls, self.applied, self.dry_run_mutation = plan, [], False, None
        self.fail_apply = False
        self.collections, self.objects = {}, {}
        self.bad_uid, self.bad_owner = False, False
        images = plan["cilium"]["images"]
        nodes, pods = [], []
        for index, node in enumerate(("worker-a", "worker-b")):
            obj = {"metadata": {"name": node, "uid": node + "-uid", "labels": {**plan["serving_selector"], "kubernetes.io/hostname": node,
                    "topology.kubernetes.io/zone": "zone-" + str(index)}}, "status": {"conditions": conditions("Ready")}}
            self.objects["node/" + node] = obj
            nodes.append(obj)
            for role in ("agent", "envoy", "operator"):
                pods.append(pod(role, node, images[role], "ReplicaSet" if role == "operator" else "DaemonSet", role + "-uid"))
        self.collections["nodes"] = nodes
        self.collections["pods"] = pods
        self.collections["replicasets"] = [{"metadata": {"uid": "operator-uid", "ownerReferences": owner("Deployment", "deployment-uid"),
            "annotations": {"deployment.kubernetes.io/revision": "1"}}}]
        for role, name in (("agent", "cilium"), ("envoy", "cilium-envoy")):
            self.objects["daemonset/" + name] = {"metadata": {"uid": role + "-uid", "generation": 1},
                "spec": {"template": {"spec": {"hostNetwork": True, "containers": [{"image": images[role]}]}}},
                "status": {"observedGeneration": 1, "desiredNumberScheduled": 2, "currentNumberScheduled": 2,
                           "updatedNumberScheduled": 2, "numberReady": 2, "numberAvailable": 2}}
        self.objects["deployment/cilium-operator"] = {"metadata": {"uid": "deployment-uid", "generation": 1, "annotations": {"deployment.kubernetes.io/revision": "1"}},
            "spec": {"replicas": 2, "template": {"spec": {"containers": [{"image": images["operator"]}]}}},
            "status": {"observedGeneration": 1, "replicas": 2, "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2}}
        self.objects["configmap/cilium-config"] = {"data": {key: "true" for key in (
            "kube-proxy-replacement", "enable-l7-proxy", "enable-gateway-api", "enable-envoy-config", "external-envoy-proxy",
            "gateway-api-hostnetwork-enabled", "enable-gateway-api-alpn", "enable-gateway-api-app-protocol")}}
        self.objects["configmap/cilium-config"]["data"].update({"enable-gateway-api-proxy-protocol": "false",
            "gateway-api-hostnetwork-nodelabelselector": ",".join(k + "=" + v for k, v in plan["serving_selector"].items())})
        self.objects["namespace/kube-system"] = {"metadata": {"uid": plan["management_target"]["cluster_uid"]}}
        self.objects["namespace/" + plan["namespace"]] = {"metadata": {"name": plan["namespace"]}}
        crds = renderer.required_crds(plan["cilium"]["gateway_api_version"], tls_passthrough="tls_passthrough_port" in plan)
        for crd, version in {**crds, "ciliumnetworkpolicies.cilium.io": "v2"}.items():
            self.objects["customresourcedefinition/" + crd] = {"metadata": {"annotations": {"gateway.networking.k8s.io/bundle-version": "v" + plan["cilium"]["gateway_api_version"]}},
                "spec": {"versions": [{"name": version, "served": True}]}, "status": {"conditions": conditions("Established")}}
        for route in plan["routes"]:
            self.objects["namespace/" + route["namespace"]] = {"metadata": {"labels": {renderer.ACCESS: plan["name"]}}}
            port = {"port": route["port"], "targetPort": route["target_port"]}
            if route["protocol"] == "h2c": port["appProtocol"] = "kubernetes.io/h2c"
            self.objects["service/" + route["service"]] = {"metadata": {"uid": route["service"] + "-uid"}, "spec": {"selector": route["pod_selector"], "ports": [port]}}
        self.desired = renderer.objects(plan)
        self.desired_keys = set()
        for obj in self.desired:
            obj["metadata"].update(generation=1, uid=obj["kind"] + "-" + obj["metadata"]["name"])
            obj["status"] = {"conditions": conditions("Accepted", "Programmed")}
            kind = obj["kind"]
            if kind == "Gateway":
                obj["status"]["listeners"] = [
                    {"name": name, "attachedRoutes": sum(renderer.listener_name(route) == name for route in plan["routes"]),
                     "conditions": conditions("Accepted", "ResolvedRefs", "Programmed")}
                    for name in renderer.listener_ports(plan)]
            if kind in renderer.ROUTE_KINDS.values():
                obj["status"] = {"parents": [{"controllerName": renderer.CONTROLLER, "parentRef": copy.deepcopy(obj["spec"]["parentRefs"][0]),
                                              "conditions": conditions("Accepted", "ResolvedRefs")}]}
            group = obj["apiVersion"].split("/")[0]
            key = kind.lower() + "." + group + "/" + obj["metadata"]["name"]
            self.objects[key] = obj
            self.desired_keys.add(key)

    @contextlib.contextmanager
    def frozen(self):
        yield self

    def call(self, args, *, payload=None, seconds=30):
        self.calls.append((args, payload))
        if args[:2] == ["config", "view"]:
            name = self.plan["management_target"]["context"]
            return json.dumps({"current-context": name, "contexts": [{"name": name, "context": {"cluster": "selected"}}],
                               "clusters": [{"name": "selected", "cluster": {"server": "https://api.example.com:6443"}}]})
        if args[0] == "apply":
            if "--dry-run=server" in args:
                if self.dry_run_mutation: self.dry_run_mutation()
            else:
                self.applied = True
                if self.fail_apply: raise ValueError("Simulated lost acknowledgment after a write")
            return ""
        if args[0] == "get" and args[1].startswith("secret/"):
            if args[-1] != "jsonpath={.type}": raise AssertionError("Secret contents must not be captured")
            return "kubernetes.io/tls"
        if args[0] == "get":
            resource = args[1]
            if resource == "gateways.gateway.networking.k8s.io":
                values = [self.objects["gateway.gateway.networking.k8s.io/" + self.plan["name"]]] if self.applied else []
                values += self.collections.get(resource, [])
            elif resource == "endpointslices.discovery.k8s.io":
                namespace = args[args.index("--namespace") + 1]
                route = next(r for r in self.plan["routes"] if r["namespace"] == namespace)
                values = self.collections.get(resource, [{"metadata": {"ownerReferences": owner("Service", route["service"] + "-uid")},
                    "ports": [{"port": route["target_port"]}], "endpoints": [{"conditions": {"ready": True}}]}])
            else:
                values = self.collections[resource]
            return json.dumps({"items": values})
        raise AssertionError("Unexpected fake command")

    def get(self, resource, namespace=None, *, optional=False):
        self.calls.append((["get", resource, namespace], None))
        if resource in self.desired_keys and not self.applied:
            if self.bad_owner: return {"metadata": {"labels": {renderer.OWNER: "somebody-else"}}}
            return None
        obj = copy.deepcopy(self.objects[resource])
        if resource == "namespace/kube-system" and self.bad_uid: obj["metadata"]["uid"] = "another-cluster"
        return obj


def package(root, runtime="rke2"):
    plan = fixture()
    files = renderer.render(plan, runtime)
    for key, value in files.items(): (root / key).write_text(value, encoding="utf-8", newline="\n")
    return plan, json.loads(files["render-status.json"])["contract_sha256"]


class GatewayTests(unittest.TestCase):
    def test_passthrough_crd_listener_and_status_for_both_compatibility_lanes(self):
        for version, gateway_version in (("1.18.3", "1.2.0"), ("1.19.10", "1.4.1")):
            for mixed in (False, True):
                with self.subTest(gateway_version=gateway_version, mixed=mixed):
                    plan = fixture()
                    plan["cilium"].update(version=version, gateway_api_version=gateway_version)
                    plan["tls_passthrough_port"] = 9443
                    plan["routes"].append(gerrit_route())
                    if not mixed:
                        del plan["https_port"], plan["tls_secret"]
                        plan["routes"] = [gerrit_route()]
                    files = renderer.render(plan, "k3s")
                    resources = list(yaml.safe_load_all(files["gateway-resources.yaml"]))
                    listeners = resources[1]["spec"]["listeners"]
                    self.assertEqual(listeners[-1]["tls"], {"mode": "Passthrough"})
                    self.assertEqual(listeners[-1]["protocol"], "TLS")
                    tls_route = next(obj for obj in resources if obj["kind"] == "TLSRoute")
                    self.assertEqual(tls_route["apiVersion"], renderer.GROUP + "/v1alpha2")
                    self.assertEqual(tls_route["spec"]["rules"][0]["backendRefs"][0]["port"], 443)
                    self.assertEqual(tls_route["spec"]["parentRefs"][0]["sectionName"], "tls-passthrough")
                    client = FakeClient(plan)
                    client.applied = True
                    contract = json.loads(files["deployment-contract.json"])
                    deploy.preflight(client, contract)
                    self.assertEqual(any("secret/" in str(args) for args, _ in client.calls), mixed)
                    self.assertTrue(deploy.ready(client, plan))
                    obj = client.objects["tlsroute." + renderer.GROUP + "/review"]
                    obj["status"]["parents"][0]["parentRef"]["sectionName"] = "https"
                    self.assertFalse(deploy.ready(client, plan))
                    client.objects["customresourcedefinition/" + renderer.TLSROUTE_CRD] = {}
                    with self.assertRaisesRegex(ValueError, "CRD"):
                        deploy.preflight(client, contract)

    def test_passthrough_port_cannot_share_an_existing_listener_port(self):
        plan = fixture()
        plan["routes"].append(gerrit_route())
        plan["tls_passthrough_port"] = plan["https_port"]
        with self.assertRaisesRegex(ValueError, "distinct unprivileged port"):
            renderer.validate(plan)
        plan["tls_passthrough_port"] = 9443
        client = FakeClient(plan)
        client.collections["gateways." + renderer.GROUP] = [
            {"metadata": {"name": "other", "namespace": "other-gateway"}, "spec": {"listeners": [{"port": 9443}]}}]
        with self.assertRaisesRegex(ValueError, "Another Gateway"):
            deploy.preflight(client, {"plan": plan, "inventory": []})
        self.assertTrue(any(args[:2] == ["get", "gateways." + renderer.GROUP] and "--all-namespaces" in args
                            for args, _ in client.calls))

    def test_named_target_port_binds_all_ready_endpoint_ports_and_original_pods(self):
        for fault in (None, "slice-port", "service-port-name", "pod-uid", "pod-port", "pod-selector", "missing-pod-ref", "mixed-ready-slices"):
            with self.subTest(fault=fault):
                plan = fixture(); route = plan["routes"][0]; client = FakeClient(plan)
                service = client.objects["service/" + route["service"]]
                port = service["spec"]["ports"][0]; port.update(name="web", targetPort="http")
                backend = pod("backend", "worker-a", "unused-image", "ReplicaSet", "rs")
                backend["metadata"].update(uid="original-backend", labels=route["pod_selector"])
                backend["spec"]["containers"][0]["ports"] = [{"name": "http", "containerPort": route["target_port"]}]
                client.objects["pod/backend"] = backend
                item = {"metadata": {"ownerReferences": owner("Service", service["metadata"]["uid"])},
                    "ports": [{"name": "web", "port": route["target_port"], "protocol": "TCP"}],
                    "endpoints": [{"conditions": {"ready": True}, "targetRef": {"kind": "Pod", "name": "backend", "namespace": route["namespace"], "uid": "original-backend"}}]}
                slices = [item]
                if fault == "slice-port": item["ports"][0]["port"] += 1
                if fault == "service-port-name": item["ports"][0]["name"] = "another-port"
                if fault == "pod-uid": backend["metadata"]["uid"] = "replacement-backend"
                if fault == "pod-port": backend["spec"]["containers"][0]["ports"][0]["containerPort"] += 1
                if fault == "pod-selector": backend["metadata"]["labels"] = {}
                if fault == "missing-pod-ref": del item["endpoints"][0]["targetRef"]
                if fault == "mixed-ready-slices":
                    extra = copy.deepcopy(item); extra["ports"][0]["port"] += 1; slices.append(extra)
                client.collections["endpointslices.discovery.k8s.io"] = slices
                if fault:
                    with self.assertRaises(ValueError): deploy.backend_endpoints(client, route, service, port)
                else: deploy.backend_endpoints(client, route, service, port)
                self.assertFalse(any(args[0] == "apply" for args, _ in client.calls))

    def test_new_lane_preflight_requires_its_six_crds_and_exact_release_annotations(self):
        plan = fixture(); plan["cilium"].update(version="1.19.10", gateway_api_version="1.4.1")
        renderer.validate(plan); client = FakeClient(plan)
        deploy.preflight(client, {"plan": plan, "inventory": []})
        name = "customresourcedefinition/backendtlspolicies.gateway.networking.k8s.io"
        client.objects[name]["metadata"]["annotations"]["gateway.networking.k8s.io/bundle-version"] = "v1.2.0"
        with self.assertRaises(ValueError): deploy.preflight(client, {"plan": plan, "inventory": []})
        client.objects[name] = {}
        with self.assertRaises(ValueError): deploy.preflight(client, {"plan": plan, "inventory": []})

    def test_all_host_runtimes_retain_explicit_kubernetes_target_and_scoped_routes(self):
        plan = fixture()
        for runtime in renderer.RUNTIMES:
            files = renderer.render(plan, runtime)
            status = json.loads(files["render-status.json"])
            self.assertEqual(status["runtime"], runtime)
            self.assertFalse(status["ha_verified"])
            self.assertFalse(status["cni_installed"])
            resources = list(yaml.safe_load_all(files["gateway-resources.yaml"]))
            self.assertEqual({obj["kind"] for obj in resources}, {"GatewayClass", "Gateway", "HTTPRoute", "GRPCRoute", "CiliumNetworkPolicy"})
            self.assertFalse(any(obj["kind"] in {"Secret", "Namespace", "DaemonSet", "Deployment"} for obj in resources))
            gateway = next(obj for obj in resources if obj["kind"] == "Gateway")
            listener = gateway["spec"]["listeners"][0]
            self.assertEqual(listener["tls"]["mode"], "Terminate")
            self.assertEqual(listener["allowedRoutes"]["namespaces"]["selector"]["matchLabels"], {renderer.ACCESS: plan["name"]})
            for policy in (obj for obj in resources if obj["kind"] == "CiliumNetworkPolicy"):
                self.assertEqual(policy["spec"]["ingress"][0]["fromEntities"], ["ingress"])
                self.assertNotIn("egress", policy["spec"])

    def test_bundled_rke2_config_preserves_pins_and_uses_local_api(self):
        plan = fixture()
        plan["cilium"].update(version="1.19.4", gateway_api_version="1.4.1")
        with self.assertRaises(ValueError):
            renderer.rke2_helmchartconfig(plan)
        plan["cilium"]["version"] = "1.19.10"
        config = renderer.rke2_helmchartconfig(plan)
        self.assertEqual(config["apiVersion"], "helm.cattle.io/v1")
        self.assertEqual(config["metadata"], {"name": "rke2-cilium", "namespace": "kube-system"})
        values = yaml.safe_load(config["spec"]["valuesContent"])
        self.assertEqual(values.pop("k8sServiceHost"), "127.0.0.1")
        self.assertEqual(values.pop("k8sServicePort"), 6443)
        self.assertEqual(values.pop("cni"), {"chainingMode": "none"})
        self.assertEqual(values, renderer.helm_overlay(plan))
        plan["cilium"]["namespace"] = "another-cilium"
        with self.assertRaises(ValueError):
            renderer.rke2_helmchartconfig(plan)

    def test_invalid_unpinned_or_conflicting_plans_stop_before_rendering(self):
        faults = [lambda p: p["cilium"]["images"].update(envoy="quay.io/cilium/cilium-envoy:latest"),
                  lambda p: p["cilium"].update(version="1.20.0"), lambda p: p.update(serving_selector={}),
                  lambda p: p.update(https_port=443), lambda p: p["management_target"].update(cluster_uid=""),
                  lambda p: p["routes"].append(copy.deepcopy(p["routes"][0])),
                  lambda p: p["routes"][0].update(pod_selector={}), lambda p: p["routes"][0].update(protocol="https"),
                  lambda p: p.update(serving_selector={"/bad": "true"}),
                  lambda p: p["routes"][0].update(name="x" * 56)]
        for fault in faults:
            plan = fixture()
            fault(plan)
            with self.assertRaises(ValueError): renderer.render(plan, "rke2")

    def test_local_inspection_never_constructs_a_kubernetes_client(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, digest = package(root)
            with mock.patch.object(deploy, "Client") as client:
                self.assertTrue(deploy.operate(root, digest)["read_only"])
            client.assert_not_called()

    def test_modified_manifest_or_contract_is_not_accepted_by_hash_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, digest = package(root)
            path = root / "gateway-resources.yaml"
            path.write_text(path.read_text() + "\n---\nkind: Secret\n", encoding="utf-8")
            contract = json.loads((root / "deployment-contract.json").read_text())
            contract["files_sha256"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            payload = json.dumps(contract, sort_keys=True, indent=2) + "\n"
            (root / "deployment-contract.json").write_text(payload, encoding="utf-8")
            with self.assertRaises(ValueError): deploy.load_package(root, hashlib.sha256(payload.encode()).hexdigest())

    def test_check_apply_and_verify_have_separate_authority_and_durable_proofs(self):
        for mode in ("check_server", "apply", "verify"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan, digest = package(root)
                client = FakeClient(plan)
                client.applied = mode == "verify"
                with mock.patch.object(deploy, "ROOT", root), mock.patch.object(deploy, "Client", return_value=client):
                    result = deploy.operate(root, digest, **{mode: True}, output=root / "private" / "proof")
                writes = [args for args, _ in client.calls if args[0] == "apply" and "--dry-run=server" not in args]
                self.assertEqual(len(writes), int(mode == "apply"))
                if mode == "verify": self.assertFalse(any(args[0] == "apply" for args, _ in client.calls))
                self.assertEqual(result["gateway_programmed"], mode != "check_server")
                self.assertFalse(result["ha_verified"])
                proof = json.loads((root / "private" / "proof" / "gateway-proof.json").read_text())
                self.assertEqual(proof["phase"], "completed")
                self.assertEqual(proof["serving_domains"], 2)

    def test_cluster_settings_topology_backend_and_ownership_fail_before_any_write(self):
        for fault in ("cluster", "kpr", "selector", "envoy", "agent-generation", "operator-domain", "node-domain", "namespace", "service", "endpoint", "endpoint-owner", "owner", "crd", "port-conflict"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan, digest = package(root)
                client = FakeClient(plan)
                if fault == "cluster": client.bad_uid = True
                if fault == "owner": client.bad_owner = True
                if fault == "kpr": client.objects["configmap/cilium-config"]["data"]["kube-proxy-replacement"] = "false"
                if fault == "selector": client.objects["configmap/cilium-config"]["data"]["gateway-api-hostnetwork-nodelabelselector"] = "different=true"
                if fault == "envoy": client.collections["pods"] = [p for p in client.collections["pods"] if p["metadata"]["name"] != "envoy-worker-a"]
                if fault == "agent-generation": client.objects["daemonset/cilium"]["status"]["observedGeneration"] = 0
                if fault == "operator-domain":
                    next(p for p in client.collections["pods"] if p["metadata"]["name"] == "operator-worker-b")["spec"]["nodeName"] = "worker-a"
                if fault == "node-domain": client.collections["nodes"][1]["metadata"]["labels"]["topology.kubernetes.io/zone"] = "zone-0"
                if fault == "namespace": client.objects["namespace/forgejo"]["metadata"]["labels"] = {}
                if fault == "service": client.objects["service/argocd-server"]["spec"]["ports"][0]["appProtocol"] = "https"
                if fault == "endpoint": client.collections["endpointslices.discovery.k8s.io"] = []
                if fault == "endpoint-owner": client.collections["endpointslices.discovery.k8s.io"] = [{"metadata": {"ownerReferences": owner("Service", "other-service-uid")},
                    "ports": [{"port": 3000}], "endpoints": [{"conditions": {"ready": True}}]}]
                if fault == "crd": client.objects["customresourcedefinition/gateways.gateway.networking.k8s.io"]["metadata"]["annotations"]["gateway.networking.k8s.io/bundle-version"] = "v9.0.0"
                if fault == "port-conflict": client.collections["gateways.gateway.networking.k8s.io"] = [{"metadata": {"name": "other", "namespace": "other"}, "spec": {"listeners": [{"port": 8443}]}}]
                with mock.patch.object(deploy, "ROOT", root), mock.patch.object(deploy, "Client", return_value=client), self.assertRaises(ValueError):
                    deploy.operate(root, digest, apply=True, output=root / "private" / "proof")
                self.assertFalse(any(args[0] == "apply" for args, _ in client.calls))
                self.assertEqual(json.loads((root / "private" / "proof" / "gateway-proof.json").read_text())["phase"], "failed")

    def test_ready_rejects_stale_conditions_wrong_parent_and_live_spec_drift(self):
        for fault in ("generation", "parent", "backend", "policy", "attached"):
            plan = fixture()
            client = FakeClient(plan)
            client.applied = True
            route = client.objects["httproute.gateway.networking.k8s.io/forge"]
            if fault == "generation": route["metadata"]["generation"] = 2
            if fault == "parent": route["status"]["parents"][0]["controllerName"] = "other/controller"
            if fault == "backend": route["spec"]["rules"][0]["backendRefs"][0]["name"] = "another-service"
            if fault == "policy": client.objects["ciliumnetworkpolicy.cilium.io/forge-gateway"]["spec"]["ingress"][0]["fromEntities"] = ["world"]
            if fault == "attached": client.objects["gateway.gateway.networking.k8s.io/platform-edge"]["status"]["listeners"][0]["attachedRoutes"] = 0
            self.assertFalse(deploy.ready(client, plan), fault)

    def test_apply_retains_verified_in_memory_bytes_and_timeout_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, digest = package(root)
            original = (root / "gateway-resources.yaml").read_bytes()
            client = FakeClient(plan)
            client.dry_run_mutation = lambda: (root / "gateway-resources.yaml").write_text("changed-after-review", encoding="utf-8")
            with mock.patch.object(deploy, "ROOT", root), mock.patch.object(deploy, "Client", return_value=client), \
                 mock.patch.object(deploy, "ready", return_value=False), mock.patch.object(deploy.time, "monotonic", side_effect=[0, 181]), self.assertRaises(ValueError):
                deploy.operate(root, digest, apply=True, output=root / "private" / "proof")
            actual = [payload for args, payload in client.calls if args[0] == "apply" and "--dry-run=server" not in args]
            self.assertEqual(actual, [original])
            proof = json.loads((root / "private" / "proof" / "gateway-proof.json").read_text())
            self.assertEqual(proof["failed_at"], "resources-applied")
            self.assertTrue(proof["deployed"])
            self.assertFalse(proof["gateway_programmed"])
            self.assertFalse(any(args[0] in {"delete", "patch", "rollout"} for args, _ in client.calls))

    def test_cli_errors_do_not_dump_private_plan_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "plan.json"
            path.write_text('{"private":"do-not-print-this"}', encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                code = renderer.main(["--plan", str(path), "--runtime", "raw", "--output-dir", str(root / "output")])
            self.assertEqual(code, 1)
            self.assertNotIn("do-not-print-this", output.getvalue())

    def test_management_snapshot_rejects_dynamic_credentials_and_restores_prefix(self):
        config = {"users": [{"user": {"exec": {"command": "must-not-run"}}}], "clusters": [{"cluster": {"server": "https://api.example.com"}}]}
        client = management.Client.__new__(management.Client)
        original = ["kubectl", "--kubeconfig", "original", "--context", "management"]
        client.prefix = original
        with mock.patch.object(client, "call", return_value=json.dumps(config)), self.assertRaises(ValueError):
            with client.frozen(): self.fail("Dynamic credentials must not enter the server phase")
        self.assertIs(client.prefix, original)
        config["users"][0]["user"] = {"token": "local-static-fixture"}
        snapshot = None
        with mock.patch.object(client, "call", return_value=json.dumps(config)), self.assertRaisesRegex(ValueError, "fixture-error"):
            with client.frozen():
                snapshot = Path(client.prefix[2])
                self.assertTrue(snapshot.is_file())
                self.assertEqual(json.loads(snapshot.read_text())["users"][0]["user"]["token"], "local-static-fixture")
                if os.name == "posix": self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
                raise ValueError("fixture-error")
        self.assertIs(client.prefix, original)
        self.assertFalse(snapshot.exists())

    def test_real_kubectl_config_snapshot_survives_source_rotation_without_api_contact(self):
        if not shutil.which("kubectl"): self.skipTest("kubectl config view is optional for this offline fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ca.crt").write_text("public-ca-fixture", encoding="utf-8")
            target = fixture()["management_target"]
            config = {"apiVersion": "v1", "kind": "Config", "current-context": "management",
                "contexts": [{"name": "management", "context": {"cluster": "selected", "user": "operator"}}],
                "clusters": [{"name": "selected", "cluster": {"server": "https://api.example.com:6443", "certificate-authority": "ca.crt"}}],
                "users": [{"name": "operator", "user": {"token": "offline-fixture-token"}}]}
            path = root / "kubeconfig"
            path.write_text(json.dumps(config), encoding="utf-8")
            with mock.patch.dict(os.environ, {target["kubeconfig_env"]: str(path)}):
                client = management.Client(target)
                with client.frozen():
                    captured = json.loads(Path(client.prefix[2]).read_text())
                    self.assertIn("certificate-authority-data", captured["clusters"][0]["cluster"])
                    path.write_text("invalid-after-rotation", encoding="utf-8")
                    current = json.loads(client.call(["config", "view", "--minify", "-o", "json"]))
                    self.assertEqual(current["clusters"][0]["cluster"]["server"], "https://api.example.com:6443")

    def test_unacknowledged_apply_is_retained_as_unknown_outcome_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, digest = package(root)
            client = FakeClient(plan)
            client.fail_apply = True
            with mock.patch.object(deploy, "ROOT", root), mock.patch.object(deploy, "Client", return_value=client), self.assertRaises(ValueError):
                deploy.operate(root, digest, apply=True, output=root / "private" / "proof")
            writes = [args for args, _ in client.calls if args[0] == "apply" and "--dry-run=server" not in args]
            self.assertEqual(len(writes), 1)
            proof = json.loads((root / "private" / "proof" / "gateway-proof.json").read_text())
            self.assertTrue(proof["apply_attempted"])
            self.assertEqual(proof["apply_outcome"], "unverified")
            self.assertEqual(proof["failed_at"], "before-apply")
            self.assertFalse(proof["gateway_programmed"])

    def test_pinned_upstream_gateway_crd_field_compatibility(self):
        source = os.environ.get("GATEWAY_API_TEST_EXPORT")
        if not source: self.skipTest("Set GATEWAY_API_TEST_EXPORT to the reviewed Gateway API v1.2.0 standard install")
        payload = Path(source).read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), "38ed055bb25dc580c0366899c0bed9b9e92dfcd1c180a569133f3946026cf102")
        crds = [obj for obj in yaml.safe_load_all(payload) if obj]
        schemas = {obj["spec"]["names"]["kind"]: next(v for v in obj["spec"]["versions"] if v["name"] == "v1")["schema"]["openAPIV3Schema"]
                   for obj in crds if any(v["name"] == "v1" for v in obj["spec"]["versions"])}
        for obj in renderer.objects(fixture()):
            if obj["kind"] in schemas: declared_fields(obj, schemas[obj["kind"]])

    def test_cilium_values_and_effective_config_key_names_match_pinned_chart_sources(self):
        source = os.environ.get("CILIUM_GATEWAY_TEST_SOURCE")
        if not source: self.skipTest("Set CILIUM_GATEWAY_TEST_SOURCE to the reviewed Cilium v1.18.3 chart source fixtures")
        root = Path(source)
        expected = {"values.yaml": "d672e772ed51e3b66673ae2997255a8cd3979b6b9f1ca08530883f85f41db339",
                    "templates-cilium-configmap.yaml": "63aaea12b63887aee3621f78badafb33dc3fe55ff2bb78eb276dcb8f634f2cf2",
                    "templates-_helpers.tpl": "4e9ae79500282a1e501bcb2772f7ec796e506c27049c46ca85967f74f2ef9eb3"}
        payloads = {}
        for name, digest in expected.items():
            data = (root / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
            payloads[name] = data.decode()
        values = yaml.safe_load(payloads["values.yaml"])
        overlay = renderer.helm_overlay(fixture())
        def declared(data, schema):
            if isinstance(data, dict):
                for key, value in data.items():
                    self.assertIn(key, schema)
                    # Node/label selectors are arbitrary user-defined maps.
                    if key not in {"matchLabels", "affinity"}: declared(value, schema[key])
        declared(overlay, values)
        config = FakeClient(fixture()).objects["configmap/cilium-config"]["data"]
        for key in config: self.assertIn(key + ":", payloads["templates-cilium-configmap.yaml"])
        self.assertIn('join "," $list', payloads["templates-_helpers.tpl"])


if __name__ == "__main__":
    unittest.main()
