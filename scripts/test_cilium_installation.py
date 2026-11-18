#!/usr/bin/env python3
"""Exercise owned CNI lifecycle transitions, interrupted writes and real chart exports."""

import contextlib
import copy
import gzip
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

import deploy_cilium_installation as deploy
import render_cilium_installation as renderer
from test_cilium_gateway import conditions, owner, pod


def fixture(release="1.18.3"):
    gateway_version = "1.4.1" if release.startswith("1.19.") else "1.2.0"
    chart = io.BytesIO()
    with tarfile.open(fileobj=chart, mode="w:gz") as archive:
        data = yaml.safe_dump({"apiVersion": "v2", "name": "cilium", "version": release, "appVersion": release}).encode()
        info = tarfile.TarInfo("cilium/Chart.yaml")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    crds = yaml.safe_dump_all([{"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
        "metadata": {"name": name, "annotations": {"gateway.networking.k8s.io/bundle-version": "v" + gateway_version}},
        "spec": {"versions": [{"name": version, "served": True}]}} for name, version in renderer.gateway.required_crds(gateway_version).items()]).encode()
    plan = {"schema_version": 1, "name": "platform-cilium", "namespace": "kube-system", "cilium_version": release,
        "management_target": {"kubeconfig_env": "PLATFORM_MANAGEMENT_KUBECONFIG", "context": "management", "cluster_uid": "11111111-1111-4111-8111-111111111111"},
        "kubernetes_version": "1.32.3", "chart_sha256": renderer.sha(chart.getvalue()), "helm_sha256": renderer.sha(b"fixture-binary"),
        "gateway_crds_sha256": renderer.sha(crds), "images": {role: "registry.example.com/cilium/" + role + ":v" + release + "@sha256:" + char * 64
            for role, char in (("agent", "a"), ("envoy", "b"), ("operator", "c"))},
        "bootstrap_api": {"host": "api.example.com", "port": 6443}, "cni_paths": {"bin": "/opt/cni/bin", "config": "/etc/cni/net.d"},
        "pod_cidr": "192.0.2.0/24", "node_cidr_mask": 28, "mtu": 1500,
        "serving_selector": {"platform.gitops/edge": "true"}, "failure_domain_key": "topology.kubernetes.io/zone"}
    if gateway_version == "1.4.1": plan.update(gateway_api_version=gateway_version, kubernetes_version="1.35.0")
    return plan, chart.getvalue(), crds


def components(plan, preflight):
    config = {key: "true" for key in ("kube-proxy-replacement", "enable-l7-proxy", "enable-gateway-api", "enable-envoy-config", "external-envoy-proxy",
              "gateway-api-hostnetwork-enabled", "enable-gateway-api-alpn", "enable-gateway-api-app-protocol")}
    config.update({"enable-gateway-api-proxy-protocol": "false", "gateway-api-hostnetwork-nodelabelselector": "platform.gitops/edge=true"})
    rows = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": plan["name"] + "-secrets", "annotations": None}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cilium-config", "namespace": "kube-system"}, "data": config}]
    for kind, role, name in (("DaemonSet", "agent", "cilium-pre-flight-check" if preflight else "cilium"),
                             ("Deployment", "operator" if not preflight else "agent", "cilium-pre-flight-check" if preflight else "cilium-operator")):
        template = {"metadata": {"labels": {"app": name}}, "spec": {"hostNetwork": True, "containers": [{"name": role, "image": plan["images"][role]}]}}
        spec = {"template": template, "selector": {"matchLabels": {"app": name}}}
        if kind == "Deployment": spec["replicas"] = 1 if preflight else 2
        rows.append({"apiVersion": "apps/v1", "kind": kind, "metadata": {"name": name, "namespace": "kube-system"}, "spec": spec})
    if preflight:
        rows.append({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "cilium-pre-flight", "namespace": "kube-system"}})
    else:
        rows.append({"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {"name": "cilium-envoy", "namespace": "kube-system"},
                     "spec": {"template": {"spec": {"hostNetwork": True, "containers": [{"name": "envoy", "image": plan["images"]["envoy"]}]}}}})
    return rows


def package(root, release="1.18.3", runtime="rke2"):
    root.mkdir(parents=True)
    plan, chart, crds = fixture(release)
    helm = root / "helm"
    helm.write_bytes(b"fixture-binary")
    def runner(args, **kwargs):
        data = yaml.safe_load(Path(args[-1]).read_text())
        self_contained = "KUBECONFIG" not in args and args[1] == "template"
        if not self_contained: raise AssertionError("Rendering must stay offline")
        return SimpleNamespace(returncode=0, stdout=yaml.safe_dump_all(components(plan, data.get("preflight", {}).get("enabled", False))))
    files = renderer.render(plan, chart, crds, runtime, helm, runner=runner)
    for name, text in files.items(): (root / name).write_text(text, encoding="utf-8", newline="\n")
    digest = json.loads(files["render-status.json"])["contract_sha256"]
    return plan, digest


def identity(resource, namespace):
    kind, name = resource.split("/", 1)
    kind = kind.split(".")[0]
    singular = {"nodes": "node", "pods": "pod", "daemonsets": "daemonset", "replicasets": "replicaset", "deployments": "deployment",
                "serviceaccounts": "serviceaccount", "clusterroles": "clusterrole", "clusterrolebindings": "clusterrolebinding"}
    return (singular.get(kind, kind), namespace, name)


class Cluster:
    def __init__(self, plan):
        self.plan, self.objects, self.calls, self.counter = plan, {}, [], 1
        self.fail_core, self.fail_reservation_ack, self.stale_preflight = False, False, False
        self.fail_namespace_ack = False
        self.nodes_ready_on_wait, self.fail_nodes_wait, self.replace_node_on_wait = False, False, False
        self.wrong_uid, self.managed, self.proxy, self.no_worker, self.on_dry_run = False, False, False, False, None
        self.put({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "kube-system", "uid": plan["management_target"]["cluster_uid"]}})
        self.put({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "kubernetes", "namespace": "default"}, "spec": {"clusterIPs": ["198.51.100.1"]}})
        for index, name in enumerate(("control-a", "control-b", "control-c", "worker-a", "worker-b")):
            labels = {"kubernetes.io/hostname": name, "topology.kubernetes.io/zone": "zone-" + str(index % 2)}
            if index < 3: labels["node-role.kubernetes.io/control-plane"] = "true"
            else: labels.update(plan["serving_selector"])
            self.put({"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "labels": labels},
                "status": {"conditions": conditions("Ready"), "nodeInfo": {"operatingSystem": "linux", "architecture": "amd64", "kernelVersion": "6.8.0"}}})

    @contextlib.contextmanager
    def frozen(self):
        yield self

    def put(self, obj):
        obj = copy.deepcopy(obj)
        meta = obj["metadata"]
        if meta.get("annotations") is None: meta.pop("annotations", None)
        ident = (obj["kind"].lower(), meta.get("namespace"), meta["name"])
        old = self.objects.get(ident, {})
        meta.update(uid=meta.get("uid") or old.get("metadata", {}).get("uid") or "uid-" + str(self.counter),
                    resourceVersion=str(self.counter), generation=old.get("metadata", {}).get("generation", 0) + 1)
        self.counter += 1
        if obj["kind"] == "DaemonSet":
            obj["status"] = {"observedGeneration": 0 if self.stale_preflight and "pre-flight" in meta["name"] else meta["generation"],
                **{key: 5 for key in ("desiredNumberScheduled", "currentNumberScheduled", "updatedNumberScheduled", "numberReady", "numberAvailable")}}
        if obj["kind"] == "Deployment":
            meta.setdefault("annotations", {})["deployment.kubernetes.io/revision"] = str(meta["generation"])
            count = obj["spec"].get("replicas", 1)
            obj["status"] = {"observedGeneration": meta["generation"], **{key: count for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")}}
        self.objects[ident] = obj

    def collection(self, kind):
        if kind == "pods":
            result = []
            for (type_, ns, _), obj in self.objects.items():
                if type_ not in {"daemonset", "deployment"}: continue
                role = "DaemonSet" if type_ == "daemonset" else "ReplicaSet"
                uid = obj["metadata"]["uid"] + ("-rs" if role == "ReplicaSet" else "")
                nodes = [o["metadata"]["name"] for (k, _, _), o in self.objects.items() if k == "node"] if role == "DaemonSet" else ["worker-a", "worker-b"][:obj["spec"].get("replicas", 1)]
                for node in nodes:
                    item = pod(obj["metadata"]["name"], node, obj["spec"]["template"]["spec"]["containers"][0]["image"], role, uid)
                    item["metadata"]["namespace"] = ns
                    result.append(item)
            if self.proxy: result.append({"metadata": {"name": "kube-proxy-node"}})
            return result
        if kind == "replicasets":
            return [{"metadata": {"name": o["metadata"]["name"] + "-rs", "uid": o["metadata"]["uid"] + "-rs", "ownerReferences": owner("Deployment", o["metadata"]["uid"]),
                "annotations": {"deployment.kubernetes.io/revision": o["metadata"]["annotations"]["deployment.kubernetes.io/revision"]}}} for (k, _, _), o in self.objects.items() if k == "deployment"]
        singular = {"nodes": "node", "daemonsets": "daemonset"}.get(kind)
        result = [o for (k, _, _), o in self.objects.items() if k == singular]
        if self.no_worker: result = [o for o in result if not o["metadata"]["name"].startswith("worker")]
        return result

    def get(self, resource, namespace=None, *, optional=False):
        self.calls.append((["get", resource, namespace], None))
        if resource.startswith("helmchart.") and self.managed: return {"metadata": {"name": "rke2-cilium"}}
        obj = copy.deepcopy(self.objects.get(identity(resource, namespace)))
        if resource == "namespace/kube-system" and self.wrong_uid: obj["metadata"]["uid"] = "other-cluster"
        if obj is None and not optional: raise ValueError("Fixture object missing")
        return obj

    def call(self, args, *, payload=None, seconds=30):
        self.calls.append((args, payload))
        if args[:2] == ["config", "view"]:
            return json.dumps({"current-context": "management", "contexts": [{"name": "management", "context": {"cluster": "selected"}}],
                "clusters": [{"name": "selected", "cluster": {"server": "https://api.example.com:6443"}}]})
        if args[0] == "version": return json.dumps({"serverVersion": {"gitVersion": "v" + self.plan["kubernetes_version"]}})
        if args[0] == "api-resources": return "helmcharts" if self.managed else ""
        if args[:2] == ["get", "secrets"]: return ""
        if args[0] == "get":
            rows = self.collection(args[1])
            if "--selector" in args:
                wanted = dict(field.split("=", 1) for field in args[args.index("--selector") + 1].split(","))
                rows = [o for o in rows if deploy.gateway.matches(o.get("metadata", {}).get("labels", {}), wanted)]
            return json.dumps({"items": rows})
        if args[0] in {"create", "replace"}:
            obj = json.loads(payload)
            ident = (obj["kind"].lower(), obj["metadata"].get("namespace"), obj["metadata"]["name"])
            old = self.objects.get(ident)
            if args[0] == "replace" and obj["metadata"].get("resourceVersion") != (old or {}).get("metadata", {}).get("resourceVersion"):
                raise ValueError("Fixture compare-and-swap conflict")
            if args[0] == "create" and old: raise ValueError("Fixture object exists")
            self.put(obj)
            if obj["kind"] == "Namespace" and self.fail_namespace_ack:
                self.fail_namespace_ack = False
                raise TimeoutError("Fixture lost namespace acknowledgment")
            if self.fail_reservation_ack:
                self.fail_reservation_ack = False
                raise TimeoutError("Fixture lost write acknowledgment")
            return "{}"
        if args[0] == "apply":
            if "--dry-run=server" in args:
                if self.on_dry_run: self.on_dry_run()
                return ""
            rows = list(yaml.safe_load_all(payload))
            for row in rows: self.put(row)
            if self.fail_core and any(o.get("kind") == "DaemonSet" and o["metadata"]["name"] == "cilium" for o in rows):
                self.fail_core = False
                raise TimeoutError("Fixture core write was accepted before acknowledgment loss")
            return ""
        if args[0] == "delete":
            parts = args[args.index("--raw") + 1].split("/")
            namespace = parts[parts.index("namespaces") + 1] if "namespaces" in parts else None
            ident = identity(parts[-2] + "/" + parts[-1], namespace)
            obj, options = self.objects[ident], json.loads(payload)
            assert options["preconditions"] == {key: obj["metadata"][key] for key in ("uid", "resourceVersion")}
            del self.objects[ident]
            return ""
        if args[:2] == ["wait", "--for=condition=Ready"]:
            if self.fail_nodes_wait: raise TimeoutError("Fixture nodes never became ready")
            for obj in self.collection("nodes"):
                if self.nodes_ready_on_wait: obj["status"]["conditions"] = conditions("Ready")
            if self.replace_node_on_wait: self.objects[("node", None, "worker-a")]["metadata"]["uid"] = "replacement-worker"
            return ""
        if args[0] in {"rollout", "wait"}: return ""
        raise AssertionError("Unexpected fixture command")


class CiliumInstallationTests(unittest.TestCase):
    def invoke(self, root, client, bundle, digest, name, **kwargs):
        with mock.patch.object(deploy, "ROOT", root), mock.patch.object(deploy, "Client", return_value=client):
            return deploy.operate(bundle, digest, output=root / "private" / name, **kwargs)

    def test_all_runtime_packages_are_explicit_kubernetes_installations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for runtime in renderer.RUNTIMES:
                plan, digest = package(root / runtime, runtime=runtime)
                contract, files = deploy.load_package(root / runtime, digest)
                self.assertEqual(contract["runtime"], runtime)
                self.assertEqual(yaml.safe_load(files["cilium-values.yaml"])["k8sServiceHost"], "api.example.com")
                with mock.patch.object(deploy, "Client") as client:
                    self.assertFalse(deploy.operate(root / runtime, digest)["bootstrap_ready"])
                client.assert_not_called()

    def test_new_lane_binds_kubernetes_gateway_crds_and_same_minor_patch_lifecycle(self):
        plan, _, definitions = fixture("1.19.10")
        renderer.validate(plan)
        self.assertEqual(len(renderer.gateway_crds(plan, definitions)), 6)
        self.assertEqual(renderer.values(plan)["upgradeCompatibility"], "1.19")
        for values in ({"gateway_api_version": "1.2.0"}, {"kubernetes_version": "1.31.9"}, {"kubernetes_version": "1.36.0"}, {"cilium_version": "1.20.0"}):
            with self.subTest(values=values), self.assertRaises(ValueError): renderer.validate({**plan, **values})
        with self.assertRaises(ValueError): renderer.validate({**fixture()[0], "kubernetes_version": "1.35.0"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, digest = package(root / "new", "1.19.10", "k3s")
            contract, _ = deploy.load_package(root / "new", digest)
            self.assertEqual(contract["plan"]["gateway_api_version"], "1.4.1")
            old = copy.deepcopy(contract); old["plan"]["cilium_version"] = "1.18.16"
            with self.assertRaisesRegex(ValueError, "Minor Cilium"): deploy.transition(contract, old, rollback=False)

    def test_tlsroute_is_an_explicit_installation_prerequisite(self):
        plan, _, definitions = fixture("1.19.10")
        tlsroute = {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
                    "metadata": {"name": renderer.gateway.TLSROUTE_CRD,
                                 "annotations": {"gateway.networking.k8s.io/bundle-version": "v1.4.1"}},
                    "spec": {"versions": [{"name": "v1alpha2", "served": True}]}}
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            renderer.gateway_crds({**plan, "tls_passthrough": True}, definitions)
        combined = definitions + b"---\n" + yaml.safe_dump(tlsroute).encode()
        plan["gateway_crds_sha256"] = renderer.sha(combined)
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            renderer.gateway_crds(plan, combined)
        plan["tls_passthrough"] = True
        self.assertEqual(renderer.gateway_crds(plan, combined)[-1], tlsroute)

    def test_namespace_preview_is_read_only_then_explicit_create_resolves_lost_ack_and_pins_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); plan, digest = package(root / "bundle", "1.19.10")
            client = Cluster(plan)
            result = self.invoke(root, client, root / "bundle", digest, "check", check_server=True)
            self.assertFalse(result["server_checked"]); self.assertTrue(result["namespace_bootstrap_checked"])
            self.assertEqual(result["namespace_bootstrap_required"], [plan["name"] + "-secrets"])
            self.assertFalse(any(args[0] in {"create", "replace", "delete"} for args, _ in client.calls))
            self.assertIsNone(client.get("namespace/" + plan["name"] + "-secrets", optional=True))
            client.fail_namespace_ack = True
            installed = self.invoke(root, client, root / "bundle", digest, "apply", apply=True)
            self.assertTrue(installed["bootstrap_ready"])
            namespace = client.get("namespace/" + plan["name"] + "-secrets")
            _, state = deploy.ledger(client, plan)
            identity = deploy.key({"apiVersion": "v1", "kind": "Namespace", "name": plan["name"] + "-secrets"})
            self.assertEqual(state["resource_uids"][identity], namespace["metadata"]["uid"])
            self.assertEqual(namespace["metadata"]["labels"][renderer.OWNER], plan["name"])

    def test_fresh_cni_waits_for_original_nodes_and_refuses_timeout_or_replacement(self):
        for fault in (None, "timeout", "replacement"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); plan, digest = package(root / "bundle", "1.19.10")
                client = Cluster(plan); client.nodes_ready_on_wait = True
                for obj in client.collection("nodes"):
                    obj["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
                client.fail_nodes_wait = fault == "timeout"
                client.replace_node_on_wait = fault == "replacement"
                if fault:
                    with self.assertRaises((ValueError, TimeoutError)):
                        self.invoke(root, client, root / "bundle", digest, "install", apply=True)
                    self.assertEqual(deploy.ledger(client, plan)[1]["phase"], "pending")
                else:
                    self.assertTrue(self.invoke(root, client, root / "bundle", digest, "install", apply=True)["bootstrap_ready"])
                waits = [args for args, _ in client.calls if args[:2] == ["wait", "--for=condition=Ready"]]
                self.assertEqual(len(waits), 1)
                self.assertEqual(set(waits[0][2:-1]), {"node/" + n for n in ("control-a", "control-b", "control-c", "worker-a", "worker-b")})

    def test_workload_api_zero_defaults_and_integer_quantities_do_not_hide_drift(self):
        desired = {"template": {"spec": {"containers": [{"name": "agent", "volumeMounts": [{"name": "runtime", "readOnly": False}],
            "readinessProbe": {"initialDelaySeconds": 0, "httpGet": {"port": 9000}}, "resources": {"limits": {"cpu": 1}}}]}}}
        actual = copy.deepcopy(desired); container = actual["template"]["spec"]["containers"][0]
        del container["volumeMounts"][0]["readOnly"]; del container["readinessProbe"]["initialDelaySeconds"]
        container["resources"]["limits"]["cpu"] = "1"
        self.assertTrue(deploy.includes(deploy.admitted_workload_spec(desired), deploy.admitted_workload_spec(actual)))
        self.assertIs(type(desired["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"]), int)
        for field in ("readOnly", "initialDelaySeconds", "cpu"):
            changed = copy.deepcopy(actual); item = changed["template"]["spec"]["containers"][0]
            if field == "readOnly": item["volumeMounts"][0][field] = True
            elif field == "initialDelaySeconds": item["readinessProbe"][field] = 1
            else: item["resources"]["limits"][field] = "2"
            with self.subTest(field=field):
                self.assertFalse(deploy.includes(deploy.admitted_workload_spec(desired), deploy.admitted_workload_spec(changed)))

    def test_loaded_new_lane_refuses_rehashed_wrong_crd_release_or_missing_served_version(self):
        for mode in ("release", "served"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "bundle"; _, digest = package(root, "1.19.10")
                payload = list(yaml.safe_load_all((root / "gateway-crds.yaml").read_text()))
                if mode == "release": payload[0]["metadata"]["annotations"]["gateway.networking.k8s.io/bundle-version"] = "v1.2.0"
                else: payload[-1]["spec"]["versions"][0]["served"] = False
                text = yaml.safe_dump_all(payload); (root / "gateway-crds.yaml").write_text(text, encoding="utf-8", newline="\n")
                contract = json.loads((root / "installation-contract.json").read_text())
                contract["files_sha256"]["gateway-crds.yaml"] = renderer.sha(text.encode())
                text = json.dumps(contract, sort_keys=True, indent=2) + "\n"; (root / "installation-contract.json").write_text(text, encoding="utf-8", newline="\n")
                with self.assertRaises(ValueError): deploy.load_package(root, renderer.sha(text.encode()))

    def test_actual_v119_chart_and_gateway_v141_export_render_for_all_host_formats(self):
        cache, helm = os.environ.get("CILIUM_INSTALL_TEST_V119_DIR"), os.environ.get("CILIUM_INSTALL_TEST_HELM")
        if not cache or not helm: self.skipTest("Explicit pinned Cilium 1.19.10/Gateway API 1.4.1 artifacts")
        plan, _, _ = fixture("1.19.10"); root = Path(cache)
        plan.update(chart_sha256="b7252afca3413a33e2b987c954e52441e96979e411e1495ecc6aefe8cd258863",
            gateway_crds_sha256="73b91b77f6be023a8c92c969fc664e5bd3b1a28aea59eac9ebc904607354dad2", helm_sha256=renderer.sha(Path(helm).read_bytes()))
        for runtime in renderer.RUNTIMES:
            files = renderer.render(plan, (root / "cilium-1.19.10.tgz").read_bytes(), (root / "gateway-api-v1.4.1-standard-install.yaml").read_bytes(), runtime, Path(helm))
            contract = json.loads(files["installation-contract.json"])
            self.assertEqual(contract["runtime"], runtime)
            self.assertEqual(len(list(yaml.safe_load_all(files["gateway-crds.yaml"]))), 6)
            self.assertEqual({row["kind"] for row in contract["inventory"]} & {"DaemonSet", "Deployment"}, {"DaemonSet", "Deployment"})

    def test_actual_v119_chart_resource_fields_survive_real_api_admission(self):
        if os.environ.get("CILIUM_INSTALL_TEST_ADMISSION") != "1": self.skipTest("Explicit disposable API admission, without workers")
        from test_rook_api_fixture import exercise
        cache, helm = Path(os.environ["CILIUM_INSTALL_TEST_V119_DIR"]), Path(os.environ["CILIUM_INSTALL_TEST_HELM"])
        def action(client, root, kubectl):
            plan, _, _ = fixture("1.19.10")
            plan.update(chart_sha256="b7252afca3413a33e2b987c954e52441e96979e411e1495ecc6aefe8cd258863",
                gateway_crds_sha256="73b91b77f6be023a8c92c969fc664e5bd3b1a28aea59eac9ebc904607354dad2", helm_sha256=renderer.sha(helm.read_bytes()),
                management_target={"kubeconfig_env": "DISPOSABLE_MANAGEMENT_CONFIG", "context": "storage", "cluster_uid": client.get("namespace/kube-system")["metadata"]["uid"]})
            files = renderer.render(plan, (cache / "cilium-1.19.10.tgz").read_bytes(), (cache / "gateway-api-v1.4.1-standard-install.yaml").read_bytes(), "k3s", helm)
            contract = json.loads(files["installation-contract.json"]); payloads = {name: text.encode() for name, text in files.items()}
            deploy.apply_file(client, payloads["gateway-crds.yaml"])
            for name in renderer.gateway.required_crds("1.4.1"):
                client.call(["wait", "--for=condition=Established", "crd/" + name, "--timeout=30s"])
            for obj in yaml.safe_load_all(files["cilium-components.yaml"]):
                if obj["kind"] == "Namespace": kubectl(["create", "-f", "-", "-o", "json"], obj)
            deploy.apply_file(client, payloads["cilium-components.yaml"])
            uids = deploy.verify_fields(client, contract, payloads)
            self.assertEqual(len(uids), len(contract["inventory"]))
            # This API has no scheduler, kubelets or Cilium processes. Its
            # evidence is admission/read-back and drift rejection only.
            obj = client.get("daemonset/cilium", "kube-system")
            self.assertEqual(obj.get("status", {}).get("numberReady", 0), 0)
            self.assertEqual(obj.get("status", {}).get("desiredNumberScheduled", 0), 0)
            client.call(["patch", "daemonset/cilium", "-n", "kube-system", "--type=json", "-p",
                '[{"op":"add","path":"/spec/template/spec/containers/0/volumeMounts/0/readOnly","value":true}]'])
            with self.assertRaises(ValueError): deploy.verify_fields(client, contract, payloads)
        exercise(self, management_action=action, privileged_admission=True)

    def test_fresh_install_then_verify_records_resource_identities_without_ha_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, digest = package(root / "bundle")
            client = Cluster(plan)
            client.fail_reservation_ack = True
            result = self.invoke(root, client, root / "bundle", digest, "install", apply=True)
            self.assertTrue(result["bootstrap_ready"])
            self.assertFalse(result["ha_verified"])
            _, state = deploy.ledger(client, plan)
            self.assertEqual(state["current"], digest)
            self.assertTrue(state["resource_uids"])
            before = len(client.calls)
            result = self.invoke(root, client, root / "bundle", digest, "verify", verify=True)
            self.assertTrue(result["read_only"])
            self.assertFalse(any(args[0] in {"create", "replace", "apply", "delete"} for args, _ in client.calls[before:]))
            before = len(client.calls)
            self.assertTrue(self.invoke(root, client, root / "bundle", digest, "idempotent", apply=True)["idempotent"])
            self.assertFalse(any(args[0] in {"create", "replace", "apply", "delete"} for args, _ in client.calls[before:]))

    def test_patch_upgrade_runs_preflight_then_uid_bound_cleanup_then_core_and_explicit_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, old = package(root / "old")
            _, new = package(root / "new", "1.18.4")
            client = Cluster(plan)
            self.invoke(root, client, root / "old", old, "install", apply=True)
            before = len(client.calls)
            self.invoke(root, client, root / "new", new, "upgrade", apply=True, previous_root=root / "old", previous_digest=old)
            calls = client.calls[before:]
            deletes = [i for i, (args, _) in enumerate(calls) if args[0] == "delete"]
            self.assertTrue(deletes)
            core = next(i for i, (args, payload) in enumerate(calls) if args[0] == "apply" and "--dry-run=server" not in args
                        and b"name: cilium\n" in payload)
            self.assertLess(max(deletes), core)
            self.assertEqual(deploy.ledger(client, plan)[1]["current"], new)
            with self.assertRaises(ValueError): self.invoke(root, client, root / "old", old, "bad-direction", apply=True, previous_root=root / "new", previous_digest=new)
            self.invoke(root, client, root / "old", old, "rollback", apply=True, rollback=True, previous_root=root / "new", previous_digest=new)
            self.assertEqual(deploy.ledger(client, plan)[1]["current"], old)

    def test_interrupted_core_apply_requires_identical_explicit_resume_and_keeps_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, digest = package(root / "bundle")
            client = Cluster(plan)
            client.fail_core = True
            with self.assertRaises(TimeoutError): self.invoke(root, client, root / "bundle", digest, "failed", apply=True)
            state = deploy.ledger(client, plan)[1]
            self.assertEqual(state["phase"], "pending")
            proof = json.loads((root / "private" / "failed" / "cilium-proof.json").read_text())
            self.assertEqual(proof["failed_at"], "before-cni-components")
            self.assertEqual(proof["write_outcome"], "unverified")
            self.assertFalse(proof["bootstrap_ready"])
            with self.assertRaises(ValueError): self.invoke(root, client, root / "bundle", digest, "implicit", apply=True)
            before = len(client.calls)
            self.invoke(root, client, root / "bundle", digest, "resume-check", check_server=True, resume=True)
            self.assertFalse(any(args[0] in {"create", "replace", "delete"} or args[0] == "apply" and "--dry-run=server" not in args for args, _ in client.calls[before:]))
            result = self.invoke(root, client, root / "bundle", digest, "resume", apply=True, resume=True)
            self.assertTrue(result["bootstrap_ready"])

    def test_wrong_target_managed_cluster_proxy_or_missing_workers_stop_before_writes(self):
        for fault in ("wrong_uid", "managed", "proxy", "no_worker"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan, digest = package(root / "bundle")
                client = Cluster(plan)
                setattr(client, fault, True)
                with self.assertRaises(ValueError): self.invoke(root, client, root / "bundle", digest, "failed", apply=True)
                self.assertFalse(any(args[0] in {"create", "replace", "apply", "delete"} for args, _ in client.calls))

    def test_stale_image_preflight_cannot_advance_installed_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, old = package(root / "old")
            _, new = package(root / "new", "1.18.4")
            client = Cluster(plan)
            self.invoke(root, client, root / "old", old, "install", apply=True)
            client.stale_preflight = True
            with self.assertRaises(ValueError): self.invoke(root, client, root / "new", new, "upgrade", apply=True, previous_root=root / "old", previous_digest=old)
            self.assertEqual(deploy.ledger(client, plan)[1]["current"], old)
            self.assertEqual(deploy.ledger(client, plan)[1]["desired"], new)

    def test_topology_change_minor_jump_and_unpinned_inputs_are_refused(self):
        plan, chart, _ = fixture()
        for change in (lambda p: p.update(cilium_version="1.19.0"), lambda p: p.update(bootstrap_api={"host": "127.0.0.1", "port": 6443}),
                       lambda p: p["images"].update(agent="quay.io/cilium/cilium:latest"), lambda p: p.update(node_cidr_mask=24),
                       lambda p: p["cni_paths"].update(bin="/")):
            candidate = copy.deepcopy(plan)
            change(candidate)
            with self.assertRaises(ValueError): renderer.validate(candidate)
        with self.assertRaises(ValueError): renderer.checked_chart(plan, chart + b"changed")
        previous = {"runtime": "rke2", "plan": plan, "inventory": []}
        target = copy.deepcopy(previous)
        target["plan"].update(cilium_version="1.18.4", mtu=1400)
        with self.assertRaises(ValueError): deploy.transition(target, previous, rollback=False)

    def test_chart_expansion_and_unsafe_archive_members_are_bounded_before_rendering(self):
        plan, _, _ = fixture()
        expanded = gzip.compress(b"x" * 4096)
        plan["chart_sha256"] = renderer.sha(expanded)
        with mock.patch.object(renderer, "MAX_ARTIFACT", 1024), self.assertRaisesRegex(ValueError, "Expanded chart"):
            renderer.checked_chart(plan, expanded)
        for name in ("../outside", "cilium/../outside", "cilium/./Chart.yaml"):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                item = tarfile.TarInfo(name)
                item.size = 1
                archive.addfile(item, io.BytesIO(b"x"))
            plan["chart_sha256"] = renderer.sha(buffer.getvalue())
            with self.assertRaises(ValueError): renderer.checked_chart(plan, buffer.getvalue())

    def test_new_reservation_after_dry_run_blocks_mutation_and_pending_target_cannot_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, digest = package(root / "bundle")
            client = Cluster(plan)
            def reserve_other():
                state = {"phase": "pending", "current": None, "desired": "f" * 64, "attempt_uid": "another-attempt", "resource_uids": {}}
                client.put({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": plan["name"] + "-installation", "namespace": plan["namespace"],
                    "labels": {renderer.OWNER: plan["name"]}}, "data": {"state": json.dumps(state)}})
            client.on_dry_run = reserve_other
            with self.assertRaises(ValueError): self.invoke(root, client, root / "bundle", digest, "conflict", apply=True)
            self.assertFalse(any(args[0] in {"create", "replace", "delete"} or args[0] == "apply" and "--dry-run=server" not in args for args, _ in client.calls))
            before = len(client.calls)
            with self.assertRaises(ValueError): self.invoke(root, client, root / "bundle", digest, "bad-resume", apply=True, resume=True)
            self.assertFalse(any(args[0] in {"create", "replace", "apply", "delete"} for args, _ in client.calls[before:]))

    def test_unowned_resources_replacement_and_runtime_drift_are_not_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, digest = package(root / "bundle")
            client = Cluster(plan)
            client.put({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cilium-config", "namespace": "kube-system"}, "data": {}})
            with self.assertRaises(ValueError): self.invoke(root, client, root / "bundle", digest, "unowned", apply=True)
            del client.objects[("configmap", "kube-system", "cilium-config")]
            self.invoke(root, client, root / "bundle", digest, "install", apply=True)
            client.objects[("daemonset", "kube-system", "cilium")]["metadata"]["uid"] = "replaced-by-other-operation"
            with self.assertRaises(ValueError): self.invoke(root, client, root / "bundle", digest, "replacement", verify=True)

    def test_real_pinned_helm_charts_render_both_patch_releases_with_the_same_lifecycle_inventory(self):
        chart_dir, helm, crds = (os.environ.get(name) for name in ("CILIUM_INSTALL_TEST_CHART_DIR", "CILIUM_INSTALL_TEST_HELM", "GATEWAY_API_TEST_EXPORT"))
        if not all((chart_dir, helm, crds)): self.skipTest("Set the local pinned chart, Helm and Gateway API fixtures for real render compatibility")
        inventories, packages = [], []
        fixture_directory = tempfile.TemporaryDirectory()
        self.addCleanup(fixture_directory.cleanup)
        workspace = Path(fixture_directory.name)
        for release, expected in (("1.18.3", "ab93b9f4bb186374f87850b154f634568ec9baab4ada0d4077ecb2a710e83067"),
                                  ("1.18.4", "c361ca567b319af64b82736ab4c5927a201b1f5dbae4e909c7aeaabce0571299")):
            plan, _, _ = fixture(release)
            chart, definitions = (Path(chart_dir) / ("cilium-" + release + ".tgz")).read_bytes(), Path(crds).read_bytes()
            self.assertEqual(renderer.sha(chart), expected)
            plan.update(chart_sha256=expected, helm_sha256=renderer.sha(Path(helm).read_bytes()), gateway_crds_sha256=renderer.sha(definitions))
            files = renderer.render(plan, chart, definitions, "rke2", Path(helm))
            contract = json.loads(files["installation-contract.json"])
            inventories.append(contract["inventory"])
            self.assertEqual({o["name"] for o in contract["preflight_inventory"] if o["kind"] in {"DaemonSet", "Deployment"}}, {"cilium-pre-flight-check"})
            root = workspace / release
            root.mkdir()
            for name, text in files.items(): (root / name).write_text(text, encoding="utf-8", newline="\n")
            digest = json.loads(files["render-status.json"])["contract_sha256"]
            deploy.load_package(root, digest)
            packages.append((root, digest, plan))
        self.assertEqual(inventories[0], inventories[1])
        old, new = packages
        client = Cluster(old[2])
        self.invoke(workspace, client, old[0], old[1], "actual-chart-install", apply=True)
        self.invoke(workspace, client, new[0], new[1], "actual-chart-upgrade", apply=True, previous_root=old[0], previous_digest=old[1])


if __name__ == "__main__":
    unittest.main()
