#!/usr/bin/env python3
"""Check scoped OpenBao creation, sealed startup and uncertain-write recovery."""

from contextlib import nullcontext
import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import deploy_openbao_ha as deploy
import render_openbao_ha as renderer
from test_keycloak_installation import cilium_template, owner, uid
from test_openbao_ha import fixture


def prepared(selected, digest):
    namespace = selected["namespace"]; config = {"enable-policy": "default", "enable-k8s-networkpolicy": "true"}
    plan = {"schema_version": 1, "contract_sha256": digest, "target": {"kubeconfig_env": "BAO_KUBECONFIG", "context": "openbao", "cluster_uid": uid(1)},
        "namespace_uid": uid(2), "node_uids": {m["node"]: uid(10 + i) for i, m in enumerate(selected["members"])},
        "secrets": {m["tls_secret"]: uid(30 + i) for i, m in enumerate(selected["members"])}, "claims": {}, "storage_classes": {},
        "cilium": {"namespace": "kube-system", "namespace_uid": uid(1), "daemonset": "cilium", "uid": uid(40), "image": cilium_template()["spec"]["containers"][0]["image"],
            "pod_template_sha256": deploy.fingerprint(cilium_template()), "configmap": "cilium-config", "config_uid": uid(41), "config_data_sha256": deploy.fingerprint(config)},
        "admitted_sha256": None}
    data = {(None, "namespace/kube-system"): {"metadata": {"uid": uid(1)}},
        (None, "namespace/" + namespace): {"metadata": {"uid": uid(2), "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}},
        ("kube-system", "configmap/cilium-config"): {"metadata": {"uid": uid(41)}, "data": config},
        ("kube-system", "daemonset/cilium"): {"metadata": {"uid": uid(40), "generation": 1}, "spec": {"template": cilium_template()},
            "status": {"observedGeneration": 1, **{k: 3 for k in ("desiredNumberScheduled", "currentNumberScheduled", "updatedNumberScheduled", "numberReady", "numberAvailable")}}}}
    for i, member in enumerate(selected["members"]):
        data[None, "node/" + member["node"]] = {"metadata": {"uid": uid(10 + i), "labels": {"kubernetes.io/hostname": member["node"], "topology.kubernetes.io/zone": member["failure_domain"]}},
            "status": {"conditions": [{"type": "Ready", "status": "True"}], "nodeInfo": {"operatingSystem": "linux"}, "addresses": [{"type": "InternalIP", "address": member["address"]}]}}
    sc = {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": "fixture-ceph-rbd", "uid": uid(60)},
        "provisioner": "rook-ceph.rbd.csi.ceph.com", "parameters": {"pool": "fixture-pool"}, "reclaimPolicy": "Retain", "volumeBindingMode": "Immediate", "allowVolumeExpansion": True}
    data[None, "storageclass/fixture-ceph-rbd"] = sc
    plan["storage_classes"]["fixture-ceph-rbd"] = {"uid": uid(60), "configuration_sha256": deploy.fingerprint(deploy.storage_configuration(sc))}
    names = [m[k] for m in selected["members"] for k in ("data_claim", "audit_claim")]
    for i, name in enumerate(names):
        volume = "fixture-volume-" + str(i)
        claim = {"metadata": {"name": name, "namespace": namespace, "uid": uid(70 + i)}, "spec": {"volumeName": volume, "storageClassName": "fixture-ceph-rbd",
            "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem", "resources": {"requests": {"storage": "2Gi"}}}, "status": {"phase": "Bound", "capacity": {"storage": "2Gi"}}}
        pv = {"metadata": {"name": volume, "uid": uid(80 + i)}, "spec": {"claimRef": {"uid": claim["metadata"]["uid"], "namespace": namespace, "name": name},
            "persistentVolumeReclaimPolicy": "Retain", "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem", "storageClassName": "fixture-ceph-rbd", "capacity": {"storage": "2Gi"},
            "csi": {"driver": sc["provisioner"], "volumeHandle": volume, "fsType": "ext4"}}, "status": {"phase": "Bound"}}
        data[namespace, "persistentvolumeclaim/" + name] = claim; data[None, "persistentvolume/" + volume] = pv
        plan["claims"][name] = {"uid": claim["metadata"]["uid"], "spec_sha256": deploy.fingerprint(claim["spec"]), "volume": volume,
            "volume_uid": pv["metadata"]["uid"], "volume_spec_sha256": deploy.fingerprint(pv["spec"]), "storage_class": "fixture-ceph-rbd"}
    return plan, data


class FakeClient:
    def __init__(self, selected, plan, data):
        self.selected = selected; self.plan = plan; self.data = copy.deepcopy(data); self.calls = []; self.created = []; self.counter = 0
        self.children = {}; self.lost_ack = None; self.unknown = None; self.mutate = None; self.started = True; self.secret_rows = {}
        self.readiness_mutator = None
        for name, identity in plan["secrets"].items(): self.secret_rows[name] = [identity, "kubernetes.io/tls", "true", "false", "true", "true", "true"]
        self.children["kube-system", "pods"] = [{"metadata": {"name": "cilium-" + str(i), "uid": uid(100 + i), "ownerReferences": owner("DaemonSet", uid(40))},
            "spec": {**copy.deepcopy(cilium_template()["spec"]), "nodeName": m["node"]}, "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{"name": "agent", "ready": True, "containerID": "containerd://fixture-cilium-" + str(i), "imageID": "fixture-cilium-image", "state": {"running": {}}}]}}
            for i, m in enumerate(selected["members"])]
    def frozen(self): return nullcontext(self)
    def get(self, resource, namespace=None, *, optional=False): return copy.deepcopy(self.data.get((namespace, resource)))
    def call(self, args, *, payload=None, seconds=30):
        self.calls.append(args)
        if args[:2] == ["config", "view"]:
            return json.dumps({"current-context": "openbao", "contexts": [{"name": "openbao", "context": {"cluster": "fixture"}}], "clusters": [{"name": "fixture", "cluster": {"server": "https://fixture.example.com"}}]})
        if args[0] == "get":
            namespace = args[args.index("--namespace") + 1] if "--namespace" in args else None
            if args[1].startswith("secret/"):
                if not any(a.startswith("go-template=") for a in args): raise AssertionError("Private Secret contents must not be captured")
                return "\n".join(self.secret_rows[args[1].split("/", 1)[1]]) + "\n"
            rows = self.children.get((namespace, args[1]), [o for (ns, _), o in self.data.items() if ns == namespace and o.get("kind", "").lower() + "s" == args[1]])
            if "--selector" in args:
                wanted = dict(s.split("=", 1) for s in args[args.index("--selector") + 1].split(","))
                rows = [o for o in rows if deploy.includes(wanted, o.get("metadata", {}).get("labels", {}))]
            return json.dumps({"items": copy.deepcopy(rows)})
        if args[0] == "create":
            obj = json.loads(payload); resource = deploy.key(obj); namespace = obj["metadata"]["namespace"]; self.counter += 1
            if obj["kind"] == "Service": obj["spec"].update(clusterIP="192.0.2." + str(self.counter), clusterIPs=["192.0.2." + str(self.counter)])
            if self.mutate: self.mutate(obj)
            if "--dry-run=server" in args: return json.dumps(obj)
            if self.unknown == resource: raise TimeoutError("Unknown create outcome")
            if (namespace, resource) in self.data: raise AssertionError("A create was repeated")
            obj["metadata"].update(uid=uid(200 + len(self.created)), generation=1); self.data[namespace, resource] = obj; self.created.append(resource)
            if obj["kind"] == "Deployment" and self.started: self.make_started(obj)
            if self.lost_ack == resource: self.lost_ack = None; raise TimeoutError("Create response was lost")
            return json.dumps(obj)
        raise AssertionError("Unexpected server operation")
    def make_started(self, obj):
        namespace = self.selected["namespace"]; member = next(m for m in self.selected["members"] if m["name"] == obj["metadata"]["name"]); i = self.selected["members"].index(member)
        obj["status"] = {"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 0}
        rs = {"metadata": {"name": member["name"] + "-rs", "uid": uid(300 + i), "generation": 1, "labels": obj["spec"]["template"]["metadata"]["labels"], "ownerReferences": owner("Deployment", obj["metadata"]["uid"])},
            "spec": {"replicas": 1, "template": copy.deepcopy(obj["spec"]["template"])}, "status": {"replicas": 1, "observedGeneration": 1}}
        pod = {"metadata": {"name": member["name"] + "-pod", "uid": uid(310 + i), "labels": rs["metadata"]["labels"], "ownerReferences": owner("ReplicaSet", rs["metadata"]["uid"])},
            "spec": {**copy.deepcopy(obj["spec"]["template"]["spec"]), "nodeName": member["node"]}, "status": {"phase": "Running", "podIP": "192.0.2." + str(121 + i),
                "conditions": [{"type": "PodScheduled", "status": "True"}], "containerStatuses": [{"name": "openbao", "ready": False, "started": True, "state": {"running": {}}, "containerID": "containerd://fixture-bao-" + str(i), "imageID": "fixture-bao-image"}]}}
        self.children.setdefault((namespace, "replicasets"), []).append(rs); self.children.setdefault((namespace, "pods"), []).append(pod)
        if self.readiness_mutator: self.readiness_mutator(pod)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup); self.root = Path(temporary.name); (self.root / "private").mkdir()
        patch = mock.patch.object(deploy, "ROOT", self.root); patch.start(); self.addCleanup(patch.stop)
        self.selected = fixture(); self.bundle = self.root / "private/bundle"; self.bundle.mkdir()
        files = renderer.render(self.selected, "k8s")
        for name, body in files.items(): (self.bundle / name).write_text(body, encoding="utf-8", newline="\n")
        self.plan, self.data = prepared(self.selected, hashlib.sha256(files["openbao-contract.json"].encode()).hexdigest())
        self.client = FakeClient(self.selected, self.plan, self.data); self.counter = 0
    def action(self, mode, output=None):
        self.counter += 1
        return deploy.operate(self.bundle, self.plan, mode=mode, output=output or self.root / "private" / ("operation-" + str(self.counter)), wait_seconds=5, client=self.client)
    def approve(self): self.plan["admitted_sha256"] = self.action("check-server")["admitted_sha256"]
    def test_local_never_constructs_a_client_or_journal(self):
        with mock.patch.object(deploy, "Client", side_effect=AssertionError("No implicit server")):
            self.assertTrue(deploy.operate(self.bundle, self.plan)["local_inputs_verified"])
        self.assertFalse(self.client.calls)
    def test_admission_is_read_only_and_does_not_capture_secret_values(self):
        self.assertTrue(self.action("check-server")["server_checked"]); self.assertFalse(self.client.created)
        self.assertTrue(all(a[0] in {"config", "get", "create"} and (a[0] != "create" or "--dry-run=server" in a) for a in self.client.calls))
    def test_apply_and_resume_observe_running_sealed_members_without_claiming_unseal(self):
        self.approve(); output = self.root / "private/original"; result = self.action("apply", output)
        self.assertTrue(result["member_processes_observed"]); self.assertTrue(result["requires_external_ceremony"])
        for flag in deploy.UNVERIFIED: self.assertIs(result[flag], False)
        self.assertEqual(len(self.client.created), 9); self.assertTrue(self.action("resume", output)["read_only"])
        self.assertEqual(len(self.client.created), 9)
        self.assertFalse(any(a[0] in {"patch", "delete", "apply", "exec"} for a in self.client.calls))
    def test_lost_create_responses_are_observed_without_repeating_writes(self):
        for kind in ("networkpolicy", "configmap", "service", "poddisruptionbudget", "deployment"):
            self.client = FakeClient(self.selected, self.plan, self.data); self.approve()
            name = next(o["metadata"]["name"] for o in deploy.load_package(self.bundle, self.plan["contract_sha256"])[1] if o["kind"].lower() == kind)
            self.client.lost_ack = kind + "/" + name; output = self.root / "private" / ("lost-" + kind)
            with self.assertRaises(TimeoutError): self.action("apply", output)
            self.assertTrue(self.action("resume", output)["member_processes_observed"])
            self.assertEqual(len(self.client.created), len(set(self.client.created))); self.assertEqual(len(self.client.created), 9)
    def test_unknown_create_is_never_reissued_and_missing_original_is_not_recreated(self):
        self.approve(); self.client.unknown = "networkpolicy/platform-openbao"; output = self.root / "private/unknown"
        with self.assertRaises(TimeoutError): self.action("apply", output)
        with mock.patch.object(deploy.time, "monotonic", side_effect=itertools.count(0, 10)), mock.patch.object(deploy.time, "sleep"):
            with self.assertRaises(TimeoutError): self.action("resume", output)
        self.assertEqual(len([a for a in self.client.calls if a[0] == "create" and "--dry-run=server" not in a]), 1)
        self.client = FakeClient(self.selected, self.plan, self.data); self.approve(); output = self.root / "private/deleted"; self.action("apply", output)
        del self.client.data[self.selected["namespace"], "service/bao-a"]
        with self.assertRaises(ValueError): self.action("resume", output)
        self.assertEqual(len(self.client.created), 9)
    def test_changed_cluster_node_tls_storage_and_cilium_refuse_before_creation(self):
        claim_name = self.selected["members"][0]["data_claim"]
        mutations = [lambda c: c.data[None, "namespace/kube-system"]["metadata"].update(uid=uid(999)),
            lambda c: c.data[None, "node/" + self.selected["members"][0]["node"]]["metadata"].update(uid=uid(999)),
            lambda c: c.secret_rows[self.selected["members"][0]["tls_secret"]].__setitem__(2, "false"),
            lambda c: c.data[self.selected["namespace"], "persistentvolumeclaim/" + claim_name]["status"].update(phase="Pending"),
            lambda c: c.data[None, "persistentvolume/fixture-volume-0"]["spec"]["claimRef"].update(uid=uid(999)),
            lambda c: c.data[None, "storageclass/fixture-ceph-rbd"].update(reclaimPolicy="Delete"),
            lambda c: c.data["kube-system", "daemonset/cilium"]["status"].update(numberReady=2)]
        for index, mutate in enumerate(mutations):
            self.client = FakeClient(self.selected, self.plan, self.data); mutate(self.client)
            with self.subTest(index=index), self.assertRaises(ValueError): self.action("check-server")
            self.assertFalse(self.client.created)
    def test_different_pv_names_must_not_alias_one_csi_volume(self):
        pv = self.client.data[None, "persistentvolume/fixture-volume-1"]
        pv["spec"]["csi"]["volumeHandle"] = "fixture-volume-0"
        claim = next(v for v in self.plan["claims"].values() if v["volume"] == "fixture-volume-1")
        claim["volume_spec_sha256"] = deploy.fingerprint(pv["spec"])
        with self.assertRaises(ValueError): self.action("check-server")
        self.assertFalse(self.client.created)
    def test_unlabelled_foreign_pod_cannot_already_use_a_reviewed_claim(self):
        self.client.children[self.selected["namespace"], "pods"] = [{"metadata": {"name": "unlabelled-foreign"},
            "spec": {"volumes": [{"name": "existing", "persistentVolumeClaim": {"claimName": self.selected["members"][0]["data_claim"]}}]}}]
        with self.assertRaises(ValueError): self.action("check-server")
        self.assertFalse(self.client.created)
    def test_running_container_before_successful_startup_probe_is_not_accepted(self):
        self.approve()
        self.client.readiness_mutator = lambda p: p["status"]["containerStatuses"][0].update(started=False)
        with mock.patch.object(deploy.time, "monotonic", side_effect=itertools.count(0, 10)), mock.patch.object(deploy.time, "sleep"):
            with self.assertRaises(TimeoutError): self.action("apply")
    def test_controller_first_revision_is_normalized_but_new_rollouts_are_refused(self):
        deployment = next(o for o in deploy.load_package(self.bundle, self.plan["contract_sha256"])[1] if o["kind"] == "Deployment")
        expected = deploy.admitted(deployment, deployment)
        observed = copy.deepcopy(deployment); observed["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": "1"}
        self.assertEqual(deploy.admitted(deployment, observed), expected)
        observed["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "2"
        with self.assertRaises(ValueError): deploy.admitted(deployment, observed)
    def test_admission_rejects_configuration_and_executable_privilege_injection(self):
        _, objects = deploy.load_package(self.bundle, self.plan["contract_sha256"])
        deployment = next(o for o in objects if o["kind"] == "Deployment")
        for mutation in (lambda p: p.update(hostPID=True), lambda p: p.update(nodeName="foreign"),
                         lambda p: p["containers"][0].update(env=[{"name": "BAO_DEV_ROOT_TOKEN_ID", "value": "<fixture-only>"}]),
                         lambda p: p["containers"][0].update(args=["-dev"]), lambda p: p["containers"][0].update(lifecycle={"postStart": {"exec": {"command": ["unexpected"]}}}),
                         lambda p: p["containers"][0]["readinessProbe"]["httpGet"].update(host="foreign.example.com"),
                         lambda p: p["containers"][0]["securityContext"].update(privileged=True)):
            changed = copy.deepcopy(deployment); mutation(changed["spec"]["template"]["spec"])
            with self.assertRaises(ValueError): deploy.admitted(deployment, changed)
        config = next(o for o in objects if o["kind"] == "ConfigMap"); changed = copy.deepcopy(config); changed["data"]["bao-a.json"] = "{}"
        with self.assertRaises(ValueError): deploy.admitted(config, changed)
    def test_changed_pod_invocation_or_original_resource_refuses_resume(self):
        for mutation in (lambda c: c.data[self.selected["namespace"], "deployment/bao-a"]["metadata"].update(uid=uid(999)),
                         lambda c: c.children[self.selected["namespace"], "pods"][0]["status"]["containerStatuses"][0].update(containerID="replacement")):
            self.client = FakeClient(self.selected, self.plan, self.data); self.approve(); output = self.root / "private" / ("original-" + str(self.counter)); self.action("apply", output); mutation(self.client)
            with self.assertRaises(ValueError): self.action("resume", output)
    def test_foreign_or_misplaced_member_cannot_complete_running_observation(self):
        self.approve(); self.client.readiness_mutator = lambda p: p["spec"].update(nodeName=self.selected["members"][0]["node"])
        with mock.patch.object(deploy.time, "monotonic", side_effect=itertools.count(0, 10)), mock.patch.object(deploy.time, "sleep"):
            with self.assertRaises(TimeoutError): self.action("apply")
    def test_package_and_source_changes_refuse_before_server_mutation(self):
        self.approve(); output = self.root / "private/source-bound"; self.action("apply", output)
        path = output / "openbao-install-proof.json"; state = json.loads(path.read_text()); state["toolset_sha256"] = "f" * 64; path.write_text(json.dumps(state))
        before = len(self.client.calls)
        with self.assertRaises(ValueError): self.action("resume", output)
        self.assertEqual(before, len(self.client.calls))
        (self.bundle / "openbao.yaml").write_text("changed")
        with self.assertRaises(ValueError): self.action("check-server")
    @unittest.skipUnless(os.environ.get("OPENBAO_INSTALL_TEST_API") == "1", "Explicit isolated real API admission/create/resume fixture")
    def test_actual_api_admission_and_original_handle_lifecycle(self):
        from test_openbao_installation_api_fixture import exercise
        exercise(self)


if __name__ == "__main__": unittest.main()
