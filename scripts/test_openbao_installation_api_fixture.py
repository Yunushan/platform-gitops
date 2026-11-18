#!/usr/bin/env python3
"""Real API lifecycle with synthetic nodes/CSI/Cilium/Pod status; no worker runtime."""

from contextlib import contextmanager
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
from unittest import mock

import deploy_openbao_ha as deploy
import render_openbao_ha as renderer
from test_openbao_installation import prepared
from test_openbao_ha import fixture
from test_rook_api_fixture import exercise as api_fixture


def exercise(test):
    def action(client, root, kubectl):
        selected = fixture(); namespace = selected["namespace"]
        def create(value): return kubectl(["create", "-f", "-", "-o", "json"], value)
        def patch(kind, name, status, ns=None):
            args = ["patch", kind + "/" + name, "--subresource=status", "--type=merge", "-p", json.dumps({"status": status}), "-o", "json"]
            if ns: args += ["--namespace", ns]
            return kubectl(args)
        create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
        for ns in (namespace, "kube-system"):
            if not client.get("serviceaccount/default", ns, optional=True): create({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "default", "namespace": ns}, "automountServiceAccountToken": False})
        private = root / "private"; private.mkdir(mode=0o700)
        bundle = private / "bundle"; bundle.mkdir(mode=0o700)
        files = renderer.render(selected, "k8s")
        for name, body in files.items(): (bundle / name).write_text(body)
        plan, data = prepared(selected, hashlib.sha256(files["openbao-contract.json"].encode()).hexdigest())
        plan["target"] = {"kubeconfig_env": "DISPOSABLE_MANAGEMENT_CONFIG", "context": "storage", "cluster_uid": client.get("namespace/kube-system")["metadata"]["uid"]}
        plan["namespace_uid"] = client.get("namespace/" + namespace)["metadata"]["uid"]
        for member in selected["members"]:
            source = data[None, "node/" + member["node"]]
            obj = create({"apiVersion": "v1", "kind": "Node", "metadata": {"name": member["node"], "labels": source["metadata"]["labels"]}})
            plan["node_uids"][member["node"]] = obj["metadata"]["uid"]; patch("node", member["node"], source["status"])
            kubectl(["patch", "node/" + member["node"], "--type=merge", "-p", '{"spec":{"taints":[]}}', "-o", "json"])
            secret = create({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": member["tls_secret"], "namespace": namespace}, "immutable": True,
                "type": "kubernetes.io/tls", "data": {k: base64.b64encode(b"fixture-not-a-real-certificate-or-key").decode() for k in ("tls.crt", "tls.key", "ca.crt")}})
            plan["secrets"][member["tls_secret"]] = secret["metadata"]["uid"]
        obj = copy.deepcopy(data[None, "storageclass/fixture-ceph-rbd"]); obj["metadata"].pop("uid")
        sc = create(obj); plan["storage_classes"]["fixture-ceph-rbd"] = {"uid": sc["metadata"]["uid"], "configuration_sha256": deploy.fingerprint(deploy.storage_configuration(sc))}
        for name, pin in plan["claims"].items():
            obj = data[namespace, "persistentvolumeclaim/" + name]
            pvc = create({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": name, "namespace": namespace}, "spec": obj["spec"]})
            patch("persistentvolumeclaim", name, obj["status"], namespace)
            pv = copy.deepcopy(data[None, "persistentvolume/" + pin["volume"]]); pv["spec"]["claimRef"]["uid"] = pvc["metadata"]["uid"]
            created = create({"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": pin["volume"]}, "spec": pv["spec"]})
            patch("persistentvolume", pin["volume"], {"phase": "Bound"})
            pvc = client.get("persistentvolumeclaim/" + name, namespace); pv = client.get("persistentvolume/" + pin["volume"])
            pin.update(uid=pvc["metadata"]["uid"], spec_sha256=deploy.fingerprint(pvc["spec"]), volume_uid=created["metadata"]["uid"], volume_spec_sha256=deploy.fingerprint(pv["spec"]))
        config = create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cilium-config", "namespace": "kube-system"}, "data": data["kube-system", "configmap/cilium-config"]["data"]})
        ds = create({"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {"name": "cilium", "namespace": "kube-system"},
            "spec": {"selector": {"matchLabels": {"k8s-app": "cilium"}}, "template": data["kube-system", "daemonset/cilium"]["spec"]["template"]}})
        c = plan["cilium"]; c.update(namespace_uid=plan["target"]["cluster_uid"], uid=ds["metadata"]["uid"], config_uid=config["metadata"]["uid"],
            pod_template_sha256=deploy.fingerprint(ds["spec"]["template"]), config_data_sha256=deploy.fingerprint(config["data"]))
        patch("daemonset", "cilium", {"observedGeneration": ds["metadata"]["generation"], **{k: 3 for k in ("desiredNumberScheduled", "currentNumberScheduled", "updatedNumberScheduled", "numberReady", "numberAvailable")}}, "kube-system")
        def status(name, index, *, ready):
            return {"phase": "Running", "podIP": "192.0.2." + str(121 + index), "conditions": [{"type": "PodScheduled", "status": "True"}, {"type": "Ready", "status": "True" if ready else "False"}],
                "containerStatuses": [{"name": name, "image": c["image"] if name == "agent" else selected["image"], "ready": ready, "started": True, "restartCount": 0,
                    "containerID": "containerd://fixture-" + name + "-" + str(index), "imageID": "sha256:" + str(index + 1) * 64,
                    "state": {"running": {"startedAt": "2026-11-07T00:00:00Z"}}}]}
        for i, member in enumerate(selected["members"]):
            obj = create({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "fixture-cilium-" + str(i), "namespace": "kube-system", "labels": {"k8s-app": "cilium"},
                "ownerReferences": [{"apiVersion": "apps/v1", "kind": "DaemonSet", "name": "cilium", "uid": ds["metadata"]["uid"], "controller": True}]},
                "spec": {**copy.deepcopy(ds["spec"]["template"]["spec"]), "nodeName": member["node"], "automountServiceAccountToken": False}})
            patch("pod", obj["metadata"]["name"], status("agent", i, ready=True), "kube-system")
        created_resources = []; lost = [True]; member_pods = []
        def synthetic_children(deployment):
            name = deployment["metadata"]["name"]; i = next(i for i, m in enumerate(selected["members"]) if m["name"] == name); member = selected["members"][i]
            rs = create({"apiVersion": "apps/v1", "kind": "ReplicaSet", "metadata": {"name": name + "-fixture", "namespace": namespace, "labels": deployment["spec"]["template"]["metadata"]["labels"],
                "ownerReferences": [{"apiVersion": "apps/v1", "kind": "Deployment", "name": name, "uid": deployment["metadata"]["uid"], "controller": True}]},
                "spec": {"replicas": 1, "selector": deployment["spec"]["selector"], "template": deployment["spec"]["template"]}})
            pod = create({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name + "-fixture", "namespace": namespace, "labels": deployment["spec"]["template"]["metadata"]["labels"],
                "ownerReferences": [{"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": rs["metadata"]["name"], "uid": rs["metadata"]["uid"], "controller": True}]},
                "spec": {**copy.deepcopy(deployment["spec"]["template"]["spec"]), "nodeName": member["node"]}})
            patch("pod", pod["metadata"]["name"], status("openbao", i, ready=False), namespace); member_pods.append(pod["metadata"]["name"])
            patch("replicaset", rs["metadata"]["name"], {"replicas": 1, "observedGeneration": rs["metadata"]["generation"]}, namespace)
            patch("deployment", name, {"replicas": 1, "updatedReplicas": 1, "observedGeneration": deployment["metadata"]["generation"]}, namespace)
        class ObservedClient:
            @contextmanager
            def frozen(self):
                with client.frozen(): yield self
            def get(self, *args, **kwargs): return client.get(*args, **kwargs)
            def call(self, args, **kwargs):
                raw = client.call(args, **kwargs)
                if args[0] == "create" and "--dry-run=server" not in args:
                    obj = json.loads(raw); created_resources.append(deploy.key(obj))
                    if obj["kind"] == "Deployment": synthetic_children(obj)
                    if obj["kind"] == "Service" and lost[0]: lost[0] = False; raise TimeoutError("Fixture lost an actual successful create response")
                return raw
        network = ObservedClient()
        with mock.patch.object(deploy, "ROOT", root):
            reviewed = deploy.operate(bundle, plan, mode="check-server", output=private / "review", wait_seconds=15, client=network)
            test.assertFalse(created_resources); plan["admitted_sha256"] = reviewed["admitted_sha256"]
            operation = private / "operation"
            with test.assertRaises(TimeoutError): deploy.operate(bundle, plan, mode="apply", output=operation, wait_seconds=15, client=network)
            result = deploy.operate(bundle, plan, mode="resume", output=operation, wait_seconds=15, client=network)
            test.assertTrue(result["member_processes_observed"]); test.assertTrue(result["requires_external_ceremony"])
            for flag in deploy.UNVERIFIED: test.assertIs(result[flag], False)
            test.assertEqual(len(created_resources), 9); test.assertEqual(len(set(created_resources)), 9)
            test.assertTrue(deploy.operate(bundle, plan, mode="resume", output=operation, wait_seconds=15, client=network)["read_only"])
            pod = client.get("pod/" + member_pods[0], namespace); changed = copy.deepcopy(pod["status"]); changed["containerStatuses"][0]["containerID"] = "containerd://replaced-fixture"
            patch("pod", member_pods[0], changed, namespace)
            with test.assertRaises(ValueError): deploy.operate(bundle, plan, mode="resume", output=operation, wait_seconds=15, client=network)
            test.assertEqual(len(created_resources), 9)
        output = os.environ.get("OPENBAO_INSTALL_TEST_REPORT")
        report = {"kubernetes_api_lifecycle_verified": True, "lost_create_response_observed_without_retry": True, "changed_member_invocation_refused": True,
            "created_resource_count": 9, "synthetic_workload_status": True, "real_workload_processes_verified": False, "initialized": False, "unsealed": False,
            "ha_verified": False, "full_stack_verified": False, "installer_toolset_sha256": deploy.toolset(), "contract_sha256": plan["contract_sha256"],
            "admitted_sha256": plan["admitted_sha256"], "original_journal_sha256": hashlib.sha256((operation / "openbao-install-proof.json").read_bytes()).hexdigest()}
        if output:
            path = Path(output).absolute(); private_root = Path(__file__).resolve().parents[1] / "private"
            if path.resolve() != path or not path.is_relative_to(private_root.resolve()) or not path.parent.is_dir(): raise ValueError("Use a new ignored private acceptance receipt")
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                json.dump(report, stream, sort_keys=True, indent=2); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        return report
    return api_fixture(test, management_action=action)
