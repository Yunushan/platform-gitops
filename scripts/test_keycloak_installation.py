#!/usr/bin/env python3
"""Exercise fresh Keycloak installation scope, partial writes and original-handle resume."""

from contextlib import nullcontext
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import deploy_keycloak_ha as deploy
import render_keycloak_ha as renderer
from test_keycloak_ha import fixture


def uid(number): return "00000000-0000-0000-0000-" + str(number).zfill(12)
def owner(kind, identity): return [{"kind": kind, "uid": identity, "controller": True}]


def cilium_template():
    return {"metadata": {"labels": {"k8s-app": "cilium"}}, "spec": {"containers": [{"name": "agent", "image": "registry.example.com/cilium@sha256:" + "c" * 64}]}}


def deployment_plan(selected, digest):
    config = {"enable-policy": "default", "enable-k8s-networkpolicy": "true"}
    return {"schema_version": 1, "contract_sha256": digest, "target": {"kubeconfig_env": "IDENTITY_KUBECONFIG", "context": "identity", "cluster_uid": uid(1)},
        "namespace_uid": uid(2), "node_uids": {m["node"]: uid(10 + i) for i, m in enumerate(selected["members"])},
        "secrets": {name: {"uid": uid(30 + i), "type": "kubernetes.io/tls" if name == selected["tls_secret"] else "Opaque"} for i, name in enumerate((selected["tls_secret"], selected["database_ca_secret"], selected["database"]["secret"]))},
        "cilium": {"namespace": "kube-system", "namespace_uid": uid(1), "daemonset": "cilium", "uid": uid(40), "image": cilium_template()["spec"]["containers"][0]["image"],
            "pod_template_sha256": deploy.fingerprint(cilium_template()), "configmap": "cilium-config", "config_uid": uid(41), "config_data_sha256": deploy.fingerprint(config)},
        "admitted_sha256": None}


class FakeClient:
    def __init__(self, selected, plan):
        self.selected, self.plan = selected, plan; self.data = {}; self.children = {}; self.created = []; self.calls = []; self.live = True
        self.lost_ack = None; self.fail_before_write = None; self.mutate = None; self.mutable_secret = False; self.allocation = 0; self.ready_mutator = None
        self.secret_snapshots = copy.deepcopy(plan["secrets"])
        self.data[(None, "namespace/kube-system")] = {"metadata": {"uid": uid(1)}}
        self.data[(None, "namespace/" + selected["namespace"])] = {"metadata": {"uid": uid(2), "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}}
        for i, member in enumerate(selected["members"]):
            self.data[(None, "node/" + member["node"])] = {"metadata": {"uid": plan["node_uids"][member["node"]], "labels": {"kubernetes.io/hostname": member["node"], "topology.kubernetes.io/zone": member["failure_domain"]}},
                "status": {"conditions": [{"type": "Ready", "status": "True"}], "nodeInfo": {"operatingSystem": "linux"}, "addresses": [{"type": "InternalIP", "address": member["address"]}]}}
        self.data[("kube-system", "configmap/cilium-config")] = {"metadata": {"uid": uid(41)}, "data": {"enable-policy": "default", "enable-k8s-networkpolicy": "true"}}
        count = len(selected["members"])
        self.data[("kube-system", "daemonset/cilium")] = {"metadata": {"uid": uid(40), "generation": 1}, "spec": {"template": cilium_template()},
            "status": {"observedGeneration": 1, **{k: count for k in ("desiredNumberScheduled", "currentNumberScheduled", "updatedNumberScheduled", "numberReady", "numberAvailable")}}}
        self.children[("kube-system", "pods")] = [self.pod("agent-" + str(i), uid(50 + i), member["node"], cilium_template()["spec"], "DaemonSet", uid(40)) for i, member in enumerate(selected["members"])]

    def pod(self, name, identity, node, spec, kind, controller):
        spec = copy.deepcopy(spec); spec["nodeName"] = node
        return {"metadata": {"name": name, "uid": identity, "ownerReferences": owner(kind, controller), "labels": renderer.LABELS}, "spec": spec,
            "status": {"phase": "Running", "podIP": "192.0.2." + str(100 + int(identity[-2:])), "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{"name": c["name"], "ready": True, "state": {"running": {}}, "containerID": "containerd://" + identity, "imageID": c["image"]} for c in spec["containers"]]}}

    def frozen(self): return nullcontext(self)

    def get(self, resource, namespace=None, optional=False):
        self.calls.append(("get", resource, namespace))
        if resource.startswith("secret/"): raise AssertionError("Never retrieve a Secret object")
        return copy.deepcopy(self.data.get((namespace, resource)))

    def call(self, args, payload=None, seconds=30):
        self.calls.append(tuple(args))
        if args[:2] == ["config", "view"]:
            return json.dumps({"current-context": "identity", "contexts": [{"name": "identity", "context": {"cluster": "selected"}}], "clusters": [{"name": "selected", "cluster": {"server": "https://api.example.com:6443"}}]})
        if args[0] == "get" and args[1].startswith("secret/"):
            if not args[-1].startswith("go-template=") or '{{index .data' in args[-1]: raise AssertionError("Secret data must not be captured")
            name = args[1].split("/", 1)[1]; pin = self.secret_snapshots[name]
            count = 2 if name == self.selected["tls_secret"] or pin["type"] == "kubernetes.io/basic-auth" else 1
            return "\n".join([pin["uid"], pin["type"], "false" if self.mutable_secret else "true", "false", *("true" for _ in range(count))]) + "\n"
        if args[0] == "get":
            namespace = args[args.index("--namespace") + 1] if "--namespace" in args else None
            resource = args[1]
            if resource in {"pods", "replicasets", "endpointslices.discovery.k8s.io"}: rows = self.children.get((namespace, resource), [])
            else: rows = [obj for (ns, _), obj in self.data.items() if ns == namespace and obj.get("kind", "").lower() + "s" == resource]
            return json.dumps({"items": copy.deepcopy(rows)})
        if args[0] == "create":
            obj = json.loads(payload); resource = deploy.key(obj); namespace = obj["metadata"]["namespace"]
            self.allocation += 1
            if obj["kind"] == "Service": obj["spec"].update(type="ClusterIP", clusterIP="192.0.2." + str(self.allocation), clusterIPs=["192.0.2." + str(self.allocation)])
            if self.mutate: self.mutate(obj)
            if "--dry-run=server" in args: return json.dumps(obj)
            if self.fail_before_write == resource: raise TimeoutError("Unknown write outcome, resource absent")
            if (namespace, resource) in self.data: raise AssertionError("A create was repeated")
            obj["metadata"].update(uid=uid(70 + len(self.created)), generation=1); self.created.append(resource); self.data[namespace, resource] = obj
            if obj["kind"] == "Deployment" and self.live: self.make_ready()
            if self.lost_ack == resource: self.lost_ack = None; raise TimeoutError("Write completed but response was lost")
            return json.dumps(obj)
        raise AssertionError("Unexpected API operation")

    def make_ready(self):
        namespace = self.selected["namespace"]; deployment = self.data[namespace, "deployment/platform-keycloak"]; count = len(self.selected["members"])
        deployment["status"] = {"observedGeneration": 1, "conditions": [{"type": "Available", "status": "True"}], **{k: count for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")}}
        self.children[namespace, "replicasets"] = [{"metadata": {"name": "platform-keycloak-rs", "uid": uid(80), "generation": 1, "ownerReferences": owner("Deployment", deployment["metadata"]["uid"])},
            "spec": {"replicas": count, "template": copy.deepcopy(deployment["spec"]["template"])}, "status": {"observedGeneration": 1, "replicas": count, "readyReplicas": count, "availableReplicas": count}}]
        pods = [self.pod("keycloak-" + str(i), uid(81 + i), member["node"], deployment["spec"]["template"]["spec"], "ReplicaSet", uid(80)) for i, member in enumerate(self.selected["members"])]
        self.children[namespace, "pods"] = pods
        self.data[namespace, "poddisruptionbudget/platform-keycloak"]["status"] = {"observedGeneration": 1, "currentHealthy": count, "expectedPods": count, "disruptionsAllowed": 1}
        service = self.data[namespace, "service/platform-keycloak"]
        self.children[namespace, "endpointslices.discovery.k8s.io"] = [{"metadata": {"uid": uid(90), "ownerReferences": owner("Service", service["metadata"]["uid"])}, "addressType": "IPv4", "ports": [{"name": "https", "port": 8443, "protocol": "TCP"}],
            "endpoints": [{"addresses": [p["status"]["podIP"]], "nodeName": p["spec"]["nodeName"], "conditions": {"ready": True}, "targetRef": {"kind": "Pod", "name": p["metadata"]["name"], "namespace": namespace, "uid": p["metadata"]["uid"]}} for p in pods]}]
        if self.ready_mutator: self.ready_mutator(self)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name); (self.root / "private").mkdir()
        patcher = mock.patch.object(deploy, "ROOT", self.root); patcher.start(); self.addCleanup(patcher.stop)
        self.selected = fixture(); self.bundle, self.plan = self.package("rke2", "bundle")
        self.client = FakeClient(self.selected, self.plan); self.counter = 0

    def package(self, runtime, name):
        directory = self.root / "private" / name; directory.mkdir()
        files = renderer.render(self.selected, runtime)
        for filename, value in files.items(): (directory / filename).write_text(value, encoding="utf-8", newline="\n")
        return directory, deployment_plan(self.selected, hashlib.sha256(files["keycloak-contract.json"].encode()).hexdigest())

    def run_action(self, mode, output=None):
        if output is None: self.counter += 1; output = self.root / "private" / ("attempt-" + str(self.counter))
        return deploy.operate(self.bundle, self.plan, mode=mode, output=output, wait_seconds=5, client=self.client)

    def approve(self): self.plan["admitted_sha256"] = self.run_action("check-server")["admitted_sha256"]

    def test_local_validation_never_constructs_a_client(self):
        with mock.patch.object(deploy, "Client", side_effect=AssertionError("No implicit server")):
            result = deploy.operate(self.bundle, self.plan)
        self.assertTrue(result["local_inputs_verified"]); self.assertTrue(result["read_only"]); self.assertFalse(self.client.calls)

    def test_check_server_captures_reviewable_material_without_writes(self):
        output = self.root / "private/check"; result = self.run_action("check-server", output)
        self.assertTrue(result["server_checked"]); self.assertTrue(deploy.sha(result["admitted_sha256"])); self.assertFalse(self.client.created)
        self.assertEqual(len(list(output.glob("*-admitted.json"))), 4)
        self.assertFalse(any(call[0] in {"patch", "delete", "apply"} for call in self.client.calls))

    def test_fresh_install_orders_policy_before_execution_and_never_certifies_ha(self):
        self.approve(); result = self.run_action("apply")
        self.assertEqual(self.client.created, ["networkpolicy/platform-keycloak", "service/platform-keycloak", "poddisruptionbudget/platform-keycloak", "deployment/platform-keycloak"])
        self.assertTrue(result["resources_ready"])
        for key in deploy.UNVERIFIED: self.assertIs(result[key], False)

    def test_all_kubernetes_choices_share_the_guarded_contract(self):
        for runtime in ("rke2", "k3s", "k8s"):
            self.bundle, self.plan = self.package(runtime, "bundle-" + runtime); self.client = FakeClient(self.selected, self.plan)
            self.approve(); self.assertTrue(self.run_action("apply")["resources_ready"])

    def test_application_hosting_backends_do_not_create_a_hidden_cluster(self):
        for runtime in ("docker", "raw"):
            bundle, plan = self.package(runtime, "bundle-" + runtime)
            with self.assertRaises(ValueError): deploy.operate(bundle, plan)

    def test_changed_bundle_is_refused_before_server_reads(self):
        (self.bundle / "new-realm-mfa.json").write_text("{}")
        with self.assertRaises(ValueError): self.run_action("apply")
        self.assertFalse(self.client.calls)

    def test_apply_requires_the_reviewed_admission_hash(self):
        with self.assertRaises(ValueError): self.run_action("apply")
        self.assertFalse(self.client.created)

    def test_unrelated_or_existing_workloads_cannot_be_adopted(self):
        self.client.children[self.selected["namespace"], "replicasets"] = [{"spec": {"template": {"metadata": {"labels": renderer.LABELS}}}}]
        with self.assertRaises(ValueError): self.run_action("check-server")
        self.assertFalse(self.client.created)

    def test_secret_metadata_read_does_not_capture_private_values(self):
        self.approve(); reads = [call for call in self.client.calls if call[0] == "get" and len(call) > 1 and str(call[1]).startswith("secret/")]
        self.assertEqual(len(reads), 3)
        self.assertTrue(all(call[-1].startswith("go-template=") and "{{if index .data" in call[-1] and "{{index .data" not in call[-1] for call in reads))
        self.client.mutable_secret = True
        with self.assertRaises(ValueError): self.run_action("apply")
        self.assertFalse(self.client.created)

    def test_existing_cnpg_basic_auth_role_snapshot_is_reused_without_copying_values(self):
        self.plan["secrets"][self.selected["database"]["secret"]]["type"] = "kubernetes.io/basic-auth"
        self.client = FakeClient(self.selected, self.plan)
        self.approve(); self.assertTrue(self.run_action("apply")["resources_ready"])
        self.assertFalse(any(call[0] in {"patch", "delete", "apply"} or call[0] == "create" and any("secret" in str(v) for v in call[1:]) for call in self.client.calls))

    def test_node_cilium_config_and_rollout_changes_refuse_before_mutation(self):
        for mutate in (lambda c: c.data[None, "node/identity-a"]["metadata"].update(uid=uid(999)),
                       lambda c: c.data["kube-system", "configmap/cilium-config"]["data"].update({"enable-policy": "never"}),
                       lambda c: c.data["kube-system", "daemonset/cilium"]["status"].update(numberReady=2)):
            self.client = FakeClient(self.selected, self.plan); mutate(self.client)
            with self.assertRaises(ValueError): self.run_action("check-server")
            self.assertFalse(self.client.created)

    def test_replaced_secret_and_wrong_cluster_cannot_use_the_original_scope(self):
        self.client.secret_snapshots[self.selected["tls_secret"]]["uid"] = uid(999)
        with self.assertRaises(ValueError): self.run_action("check-server")
        self.assertFalse(self.client.created)
        self.client = FakeClient(self.selected, self.plan)
        self.plan["target"]["cluster_uid"] = uid(999)
        with self.assertRaises(ValueError): self.run_action("check-server")
        self.assertFalse(self.client.created)

    def test_resume_refuses_a_different_source_closure(self):
        self.approve(); output = self.root / "private/source-pinned"; self.run_action("apply", output)
        path = output / "keycloak-install-proof.json"; state = json.loads(path.read_text()); state["toolset_sha256"] = "f" * 64
        path.write_text(json.dumps(state))
        before = len(self.client.calls)
        with self.assertRaises(ValueError): self.run_action("resume", output)
        self.assertEqual(len(self.client.calls), before)

    def test_dangerous_admission_and_admission_drift_refuse(self):
        self.client.mutate = lambda o: o["spec"]["template"]["spec"].update(hostPID=True) if o["kind"] == "Deployment" else None
        with self.assertRaises(ValueError): self.run_action("check-server")
        self.assertFalse(self.client.created)
        self.client.mutate = None; self.approve()
        self.client.mutate = lambda o: o["spec"].update(minAvailable=99) if o["kind"] == "PodDisruptionBudget" else None
        with self.assertRaises(ValueError): self.run_action("apply")
        self.assertFalse(self.client.created)

    def test_admission_cannot_inject_hooks_environment_or_foreign_probes(self):
        expected = renderer.kubernetes(self.selected)[0]
        for mutate in (lambda p: p.update(nodeName="foreign-host"),
                       lambda p: p.update(schedulerName="unreviewed-scheduler"),
                       lambda p: p["containers"][0].update(lifecycle={"postStart": {"exec": {"command": ["unreviewed"]}}}),
                       lambda p: p["containers"][0].update(envFrom=[{"configMapRef": {"name": "unreviewed"}}]),
                       lambda p: p["containers"][0]["readinessProbe"]["httpGet"].update(host="foreign.example.com"),
                       lambda p: p["containers"][0]["securityContext"].update(seccompProfile={"type": "Unconfined"})):
            changed = copy.deepcopy(expected); mutate(changed["spec"]["template"]["spec"])
            with self.assertRaises(ValueError): deploy.admitted(expected, changed)

    def test_ack_loss_observes_same_object_without_repeating_create(self):
        for lost in ("networkpolicy", "service", "poddisruptionbudget", "deployment"):
            self.client = FakeClient(self.selected, self.plan); self.approve(); output = self.root / "private" / ("lost-" + lost)
            self.client.lost_ack = lost + "/platform-keycloak"
            with self.assertRaises(TimeoutError): self.run_action("apply", output)
            self.assertTrue(self.run_action("resume", output)["resources_ready"])
            self.assertEqual(len(self.client.created), 4); self.assertEqual(len(set(self.client.created)), 4)

    def test_unknown_outcome_is_not_retried_if_object_is_absent(self):
        self.approve(); self.client.fail_before_write = "networkpolicy/platform-keycloak"; output = self.root / "private/unknown"
        with self.assertRaises(TimeoutError): self.run_action("apply", output)
        with mock.patch.object(deploy.time, "monotonic", side_effect=[0, 6]), mock.patch.object(deploy.time, "sleep"):
            with self.assertRaises(TimeoutError): self.run_action("resume", output)
        self.assertFalse(self.client.created)
        real_requests = [call for call in self.client.calls if call[0] == "create" and "--dry-run=server" not in call]
        self.assertEqual(len(real_requests), 1)

    def test_deleted_original_resource_is_never_recreated(self):
        self.approve(); output = self.root / "private/original"; self.run_action("apply", output)
        del self.client.data[self.selected["namespace"], "service/platform-keycloak"]
        with self.assertRaises(ValueError): self.run_action("resume", output)
        self.assertEqual(len(self.client.created), 4)

    def test_replaced_original_uid_is_refused(self):
        self.approve(); output = self.root / "private/original"; self.run_action("apply", output)
        self.client.data[self.selected["namespace"], "deployment/platform-keycloak"]["metadata"]["uid"] = uid(999)
        with self.assertRaises(ValueError): self.run_action("resume", output)

    def test_foreign_endpoint_or_same_node_placement_is_not_ready(self):
        for mutate in (lambda c: c.children[self.selected["namespace"], "pods"][1]["spec"].update(nodeName="identity-a"),
                       lambda c: c.children[self.selected["namespace"], "endpointslices.discovery.k8s.io"][0]["endpoints"][0]["targetRef"].update(uid=uid(999))):
            self.client = FakeClient(self.selected, self.plan); self.approve(); self.client.ready_mutator = mutate
            with mock.patch.object(deploy.time, "monotonic", side_effect=[0, 0, 0, 0, 0, 6]), mock.patch.object(deploy.time, "sleep"):
                with self.assertRaises(TimeoutError): self.run_action("apply")

    @unittest.skipUnless(os.environ.get("KEYCLOAK_INSTALL_TEST_API") == "1", "Explicit opt-in to the disposable API admission checker")
    def test_actual_api_admission_hash_survives_service_allocation_and_readback(self):
        from test_rook_api_fixture import exercise
        exercise(self, resource_objects=renderer.kubernetes(self.selected), admission_checker=deploy.admitted)


if __name__ == "__main__": unittest.main()
