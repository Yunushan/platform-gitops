#!/usr/bin/env python3
"""Check original Valkey resources, scoped metadata and uncertain-create custody."""

import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import deploy_valkey_ha as deploy
import render_valkey_ha as renderer
from test_openbao_installation import prepared as infrastructure_prepared,FakeClient as InfrastructureClient
from test_keycloak_installation import owner,uid
from test_valkey_ha import fixture


def prepared(selected,digest):
    projection=copy.deepcopy(selected)
    for m in projection["members"]: m.update(data_claim=m["server_claim"],audit_claim=m["sentinel_claim"])
    plan,data=infrastructure_prepared(projection,digest); plan["tls_secrets"]=plan.pop("secrets")
    plan["auth_secrets"]={m["auth_secret"]:uid(50+i) for i,m in enumerate(selected["members"])}
    return plan,data


def container_status(name,image,index,*,init=False):
    token=hashlib.sha256((name+str(index)).encode()).hexdigest()
    state={"terminated":{"exitCode":0,"startedAt":"2026-11-07T00:00:00Z","finishedAt":"2026-11-07T00:00:01Z"}} if init else {"running":{"startedAt":"2026-11-07T00:00:02Z"}}
    return {"name":name,"image":image,"imageID":"sha256:"+token,"containerID":"containerd://"+token,"restartCount":0,"ready":not init,"started":True,"state":state}


class FakeClient(InfrastructureClient):
    def __init__(self,selected,plan,data):
        super().__init__(selected,deploy.infrastructure_plan(plan),data); self.plan=plan
        self.secret_rows.update({n:[identity,"Opaque","true","false","true","true","true","true"] for n,identity in plan["auth_secrets"].items()})
    def call(self,args,*,payload=None,seconds=30):
        if args[0]!="create": return super().call(args,payload=payload,seconds=seconds)
        self.calls.append(args); obj=json.loads(payload); resource=deploy.key(obj); namespace=obj["metadata"]["namespace"]; self.counter+=1
        if obj["kind"]=="Service": obj["spec"].update(clusterIPs=["None"],type="ClusterIP",ipFamilies=["IPv4"],ipFamilyPolicy="SingleStack",sessionAffinity="None")
        if self.mutate: self.mutate(obj)
        if "--dry-run=server" in args: return json.dumps(obj)
        if self.unknown==resource: raise TimeoutError("Unknown create outcome")
        if (namespace,resource) in self.data: raise AssertionError("Create was repeated")
        obj["metadata"].update(uid=uid(200+len(self.created)),generation=1); self.data[namespace,resource]=obj; self.created.append(resource)
        if obj["kind"]=="Deployment" and self.started: self.make_started(obj)
        if self.lost_ack==resource: self.lost_ack=None; raise TimeoutError("Original create response lost")
        return json.dumps(obj)
    def make_started(self,obj):
        namespace=self.selected["namespace"]; member=next(m for m in self.selected["members"] if m["name"]==obj["metadata"]["name"]); i=self.selected["members"].index(member)
        obj["status"]={"observedGeneration":1,"replicas":1,"updatedReplicas":1,"readyReplicas":1,"availableReplicas":1}
        rs={"metadata":{"name":member["name"]+"-rs","uid":uid(300+i),"generation":1,"labels":obj["spec"]["template"]["metadata"]["labels"],"ownerReferences":owner("Deployment",obj["metadata"]["uid"])},
            "spec":{"replicas":1,"template":copy.deepcopy(obj["spec"]["template"])},"status":{"replicas":1,"readyReplicas":1,"availableReplicas":1,"observedGeneration":1}}
        pod={"metadata":{"name":member["name"]+"-pod","uid":uid(310+i),"labels":rs["metadata"]["labels"],"annotations":obj["spec"]["template"]["metadata"]["annotations"],"ownerReferences":owner("ReplicaSet",rs["metadata"]["uid"])},
            "spec":{**copy.deepcopy(obj["spec"]["template"]["spec"]),"nodeName":member["node"]},"status":{"phase":"Running","podIP":"192.0.2."+str(121+i),
                "conditions":[{"type":"PodScheduled","status":"True"},{"type":"Ready","status":"True"}],
                "containerStatuses":[container_status(k,self.selected["image"],i) for k in ("server","sentinel")],
                "initContainerStatuses":[container_status("seed-original-config",self.selected["tools_image"],i,init=True)]}}
        self.children.setdefault((namespace,"replicasets"),[]).append(rs); self.children.setdefault((namespace,"pods"),[]).append(pod)
        if self.readiness_mutator: self.readiness_mutator(pod)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); self.root=Path(temp.name); (self.root/"private").mkdir()
        patch=mock.patch.object(deploy,"ROOT",self.root); patch.start(); self.addCleanup(patch.stop)
        self.selected=fixture(); self.bundle=self.root/"private/bundle"; self.bundle.mkdir(); files=renderer.render(self.selected,"k8s")
        for name,body in files.items(): (self.bundle/name).write_text(body,encoding="utf-8",newline="\n")
        self.plan,self.data=prepared(self.selected,hashlib.sha256(files["valkey-contract.json"].encode()).hexdigest()); self.client=FakeClient(self.selected,self.plan,self.data); self.counter=0
    def action(self,mode,output=None):
        self.counter+=1
        return deploy.operate(self.bundle,self.plan,mode=mode,output=output or self.root/"private"/("operation-"+str(self.counter)),wait_seconds=5,client=self.client)
    def approve(self): self.plan["admitted_sha256"]=self.action("check-server")["admitted_sha256"]
    def test_local_does_not_construct_a_client_or_journal(self):
        with mock.patch.object(deploy,"Client",side_effect=AssertionError("No server")): self.assertTrue(deploy.operate(self.bundle,self.plan)["local_inputs_verified"])
        self.assertFalse(self.client.calls)
    def test_admission_is_read_only_and_secrets_are_only_projected_metadata(self):
        self.assertTrue(self.action("check-server")["server_checked"]); self.assertFalse(self.client.created)
        secret_reads=[a for a in self.client.calls if a[:1]==["get"] and a[1].startswith("secret/")]
        self.assertEqual(len(secret_reads),6); self.assertTrue(all(any(v.startswith("go-template=") for v in a) for a in secret_reads))
        self.assertTrue(all(a[0] in {"config","get","create"} and (a[0]!="create" or "--dry-run=server" in a) for a in self.client.calls))
    def test_apply_resume_observe_eleven_originals_and_all_three_process_handles(self):
        self.approve(); output=self.root/"private/original"; result=self.action("apply",output)
        self.assertTrue(result["member_processes_observed"]); self.assertTrue(result["completed_seed_observed"]); self.assertTrue(result["requires_runtime_verification"])
        self.assertEqual(len(self.client.created),11); self.assertTrue(self.action("resume",output)["read_only"]); self.assertEqual(len(self.client.created),11)
        for flag in deploy.UNVERIFIED: self.assertIs(result[flag],False)
        self.assertFalse(any(a[0] in {"exec","patch","delete","apply"} for a in self.client.calls))
        journal=json.loads((output/"valkey-install-proof.json").read_text())
        for m in self.selected["members"]:
            obj=self.client.get("deployment/"+m["name"],self.selected["namespace"])
            self.assertEqual(obj["spec"]["template"]["metadata"]["annotations"][deploy.OPERATION],journal["operation_id"])
    def test_lost_create_responses_are_observed_without_repeating_them(self):
        for kind in ("networkpolicy","configmap","service","poddisruptionbudget","deployment"):
            self.client=FakeClient(self.selected,self.plan,self.data); self.approve()
            obj=next(o for o in deploy.load_package(self.bundle,self.plan["contract_sha256"])[1] if o["kind"].lower()==kind); self.client.lost_ack=deploy.key(obj); output=self.root/"private"/("lost-"+kind)
            with self.assertRaises(TimeoutError): self.action("apply",output)
            self.assertTrue(self.action("resume",output)["member_processes_observed"]); self.assertEqual(len(self.client.created),11); self.assertEqual(len(set(self.client.created)),11)
    def test_unknown_create_and_missing_original_are_never_recreated(self):
        self.approve(); self.client.unknown="networkpolicy/platform-valkey-sentinel"; output=self.root/"private/unknown"
        with self.assertRaises(TimeoutError): self.action("apply",output)
        with mock.patch.object(deploy.time,"monotonic",side_effect=itertools.count(0,10)),mock.patch.object(deploy.time,"sleep"):
            with self.assertRaises(TimeoutError): self.action("resume",output)
        self.assertFalse(self.client.created)
        self.client=FakeClient(self.selected,self.plan,self.data); output=self.root/"private/missing"; self.action("apply",output); del self.client.data[self.selected["namespace"],"service/cache-a"]
        with self.assertRaises(ValueError): self.action("resume",output)
        self.assertEqual(len(self.client.created),11)
    def test_changed_auth_tls_node_cilium_and_csi_handles_fail_before_creation(self):
        changes=[lambda c:c.secret_rows["cache-a-auth"].__setitem__(0,uid(999)),lambda c:c.secret_rows["cache-a-auth"].__setitem__(2,"false"),
            lambda c:c.secret_rows["cache-a-auth"].__setitem__(7,"false"),lambda c:c.secret_rows["cache-a-tls"].__setitem__(1,"Opaque"),
            lambda c:c.data[None,"node/worker-a.example.com"]["status"]["conditions"][0].update(status="False"),
            lambda c:c.data["kube-system","configmap/cilium-config"]["data"].update({"enable-policy":"never"})]
        for i,change in enumerate(changes):
            self.client=FakeClient(self.selected,self.plan,self.data); change(self.client)
            with self.subTest(case=i),self.assertRaises(ValueError): self.action("check-server")
            self.assertFalse(self.client.created)
        self.client=FakeClient(self.selected,self.plan,self.data)
        self.client.data[None,"persistentvolume/fixture-volume-1"]["spec"]["csi"]["volumeHandle"]="fixture-volume-0"
        self.plan["claims"]["cache-a-sentinel"]["volume_spec_sha256"]=deploy.fingerprint(self.client.data[None,"persistentvolume/fixture-volume-1"]["spec"])
        with self.assertRaises(ValueError): self.action("check-server")
    def test_unlabelled_pod_using_claim_is_refused(self):
        self.client.children[self.selected["namespace"],"pods"]=[{"metadata":{"name":"foreign"},"spec":{"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"cache-a-data"}}]}}]
        with self.assertRaises(ValueError): self.action("check-server")
    def test_admission_injected_executable_annotations_and_non_headless_service_refuse(self):
        def change(obj):
            if obj["kind"]=="Deployment": obj["spec"]["template"]["spec"]["initContainers"][0]["env"].append({"name":"UNREVIEWED","value":"injection"})
        for mutate in (change,lambda obj:obj["spec"].update(clusterIP="192.0.2.99") if obj["kind"]=="Service" else None,
                lambda obj:obj["spec"]["template"]["metadata"]["annotations"].update({"network.example.com/attachment":"foreign"}) if obj["kind"]=="Deployment" else None):
            self.client=FakeClient(self.selected,self.plan,self.data); self.client.mutate=mutate
            with self.assertRaises(ValueError): self.action("check-server")
            self.assertFalse(self.client.created)
    def test_seed_completion_and_both_ready_processes_are_required(self):
        self.approve()
        for mutate in (lambda p:p["status"]["initContainerStatuses"][0]["state"]["terminated"].update(exitCode=1),
                lambda p:p["status"]["containerStatuses"][1].update(started=False),lambda p:p["status"]["containerStatuses"][1].update(ready=False)):
            self.client=FakeClient(self.selected,self.plan,self.data); self.client.readiness_mutator=mutate
            with mock.patch.object(deploy.time,"monotonic",side_effect=itertools.count(0,10)),mock.patch.object(deploy.time,"sleep"):
                with self.assertRaises(TimeoutError): self.action("apply")
    def test_seed_or_main_invocation_change_is_refused_on_resume(self):
        for kind in ("initContainerStatuses","containerStatuses"):
            self.client=FakeClient(self.selected,self.plan,self.data); self.approve(); output=self.root/"private"/("changed-"+kind); self.action("apply",output)
            self.client.children[self.selected["namespace"],"pods"][0]["status"][kind][0]["containerID"]="containerd://"+"f"*64
            with self.assertRaises(ValueError): self.action("resume",output)
            self.assertEqual(len(self.client.created),11)
    def test_package_source_and_role_secret_aliases_are_refused(self):
        self.plan["auth_secrets"]["cache-a-auth"]=self.plan["tls_secrets"]["cache-a-tls"]
        with self.assertRaises(ValueError): self.action("local")
        self.plan,self.data=prepared(self.selected,self.plan["contract_sha256"])
        (self.bundle/"valkey.yaml").write_text("changed")
        with self.assertRaises(ValueError): self.action("local")
    @unittest.skipUnless(os.environ.get("VALKEY_INSTALL_TEST_API")=="1","Explicit disposable actual API lifecycle with synthetic workload status")
    def test_actual_api_lifecycle(self):
        from test_valkey_installation_api_fixture import exercise
        exercise(self)


if __name__=="__main__": unittest.main()
