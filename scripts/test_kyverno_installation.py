#!/usr/bin/env python3
"""Opt-in Kyverno installation ownership, trust and fixed topology boundaries."""

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import deploy_kyverno_installation as deploy
import render_kyverno_installation as renderer
import yaml
from bounded_subprocess import SubprocessOutputLimitExceeded
from kyverno_admission_state import (
    budget_blocks_second_peer,
    fresh_original_lease,
    graceful_failed_controller_transition,
    ready_endpoint_uids,
)
from kyverno_policy_status import original_policy_ready
from test_kyverno_failure_fixture import bounded_failure_output, restore_original_guests


def fixture():
    plan={"schema_version":1,"runtime":"k3s","management_target":{"kubeconfig_env":"TEST_KUBECONFIG","context":"disposable","cluster_uid":"11111111-1111-4111-8111-111111111111"},
        "namespace_uid":"22222222-2222-4222-8222-222222222222","trust_configmap":{"name":"private-ca","uid":"33333333-3333-4333-8333-333333333333","ca_sha256":hashlib.sha256(b"synthetic-public-ca").hexdigest()}}
    images=iter(renderer.IMAGES)
    objects=[{"apiVersion":"v1","kind":"Namespace","metadata":{"name":"kyverno"}}]
    for name in renderer.CONTROLLERS:
        container={"name":"controller","image":next(images),"securityContext":{"runAsNonRoot":True,"allowPrivilegeEscalation":False}}
        spec={"containers":[container]}
        if name=="kyverno-admission-controller":spec["initContainers"]=[{"name":"init","image":next(images),"resources":{"requests":{"cpu":"100m","memory":"64Mi"}}}]
        objects.append({"apiVersion":"apps/v1","kind":"Deployment","metadata":{"name":name,"namespace":"kyverno"},
            "spec":{"selector":{"matchLabels":{"app.kubernetes.io/name":name}},"template":{"metadata":{"labels":{"app.kubernetes.io/name":name}},"spec":spec}}})
    objects.extend({"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"synthetic-"+str(i),"namespace":"kyverno"}} for i in range(65))
    return plan,yaml.safe_dump_all(objects).encode()


class FakeClient:
    def __init__(self,plan):self.plan=plan;self.calls=[];self.changed=None;self.foreign=False
    def frozen(self):return nullcontext(self)
    def get(self,resource,namespace=None,*,optional=False):
        if optional:return {"metadata":{"labels":{renderer.OWNER_LABEL:"foreign"}}} if self.foreign else None
        if resource=="namespace/kyverno":return {"metadata":{"uid":"unexpected" if self.changed=="namespace" else self.plan["namespace_uid"]}}
        if resource=="configmap/private-ca":return {"metadata":{"uid":"unexpected" if self.changed=="trust-uid" else self.plan["trust_configmap"]["uid"]},"immutable":self.changed!="mutable-ca","data":{"ca.crt":"changed" if self.changed=="trust-bytes" else "synthetic-public-ca"}}
        if resource.startswith("deployment.apps/"):
            name=resource.split('/')[1];desired=renderer.CONTROLLERS[name][0]
            return {"metadata":{"uid":"original-"+name,"generation":2},"spec":{"replicas":desired},"status":{"observedGeneration":2,"replicas":desired,"readyReplicas":desired,"updatedReplicas":desired,"availableReplicas":desired}}
        raise AssertionError("Unexpected remote read")
    def call(self,args,*,payload=None,seconds=30):self.calls.append((args,payload,seconds));return ""


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.plan,self.release=fixture();self.release_sha=hashlib.sha256(self.release).hexdigest()
        self.pin=mock.patch.object(renderer,"RELEASE_SHA",self.release_sha);self.pin.start();self.addCleanup(self.pin.stop)
        self.files=renderer.render(self.plan,self.release)
        for name,text in self.files.items():(self.root/name).write_text(text,encoding="utf-8",newline="\n")
        self.digest=json.loads(self.files["kyverno-installation-status.json"])["contract_sha256"]

    def test_image_policy_uses_nested_compiled_ready_for_the_original_spec(self):
        original={"metadata":{"name":"synthetic","uid":"original","generation":1},"spec":{"failurePolicy":"Fail"}}
        current=copy.deepcopy(original);current["status"]={"conditions":[{"type":"Ready","status":"True"}]}
        self.assertFalse(original_policy_ready(current,original))
        for value in (False,"True",1,None):
            current["status"]={"conditionStatus":{"ready":value}}
            self.assertFalse(original_policy_ready(current,original))
        current["status"]={"conditionStatus":{"ready":True}}
        self.assertTrue(original_policy_ready(current,original))
        for field,value in (("uid","replacement"),("generation",2),("name","different")):
            changed=copy.deepcopy(current);changed["metadata"][field]=value
            with self.assertRaises(ValueError):original_policy_ready(changed,original)
        changed=copy.deepcopy(current);changed["spec"]["failurePolicy"]="Ignore"
        with self.assertRaises(ValueError):original_policy_ready(changed,original)

    def test_admission_endpoints_require_original_owner_pod_address_and_port(self):
        service={"metadata":{"name":"kyverno-svc","namespace":"kyverno","uid":"original-service"},"spec":{"ports":[{"name":"https","targetPort":"https"}]}}
        pod={"metadata":{"name":"admission","uid":"original-pod"},"spec":{"nodeName":"worker","containers":[{"ports":[{"name":"https","containerPort":9443}]}]},"status":{"podIP":"192.0.2.50"}}
        endpoint={"metadata":{"namespace":"kyverno","labels":{"kubernetes.io/service-name":"kyverno-svc"},"ownerReferences":[{"name":"kyverno-svc","kind":"Service","uid":"original-service","controller":True}]},"addressType":"IPv4","ports":[{"name":"https","port":9443,"protocol":"TCP"}],"endpoints":[{"addresses":["192.0.2.50"],"nodeName":"worker","targetRef":{"kind":"Pod","name":"admission","namespace":"kyverno","uid":"original-pod"},"conditions":{"ready":True,"serving":True,"terminating":False}}]}
        self.assertEqual(ready_endpoint_uids([endpoint],service,[pod]),{"original-pod"})
        item=copy.deepcopy(endpoint);item["endpoints"][0]["conditions"]["ready"]=False
        self.assertFalse(ready_endpoint_uids([item],service,[pod]))
        for ready in (None,"True",1):
            item=copy.deepcopy(endpoint);item["endpoints"][0]["conditions"]["ready"]=ready
            with self.assertRaises(ValueError):ready_endpoint_uids([item],service,[pod])
        item=copy.deepcopy(endpoint);item["endpoints"][0]["conditions"].pop("ready")
        with self.assertRaises(ValueError):ready_endpoint_uids([item],service,[pod])
        for change in (lambda p:p["metadata"]["ownerReferences"][0].update(uid="replacement"),
                lambda p:p["endpoints"][0]["targetRef"].update(uid="replacement"),lambda p:p["endpoints"][0].update(addresses=["192.0.2.51"]),
                lambda p:p["endpoints"][0].update(nodeName="other"),lambda p:p["ports"][0].update(port=443),
                lambda p:p["endpoints"][0]["conditions"].update(serving=False),lambda p:p["endpoints"][0]["conditions"].update(terminating=True)):
            item=copy.deepcopy(endpoint);change(item)
            with self.assertRaises(ValueError):ready_endpoint_uids([item],service,[pod])
        with self.assertRaises(ValueError):ready_endpoint_uids([endpoint,endpoint],service,[pod])

    def test_budget_requires_successful_current_processing_of_original_spec(self):
        original={"metadata":{"name":"admission","namespace":"kyverno","uid":"original","generation":1},"spec":{"minAvailable":2}}
        current=copy.deepcopy(original);current["status"]={"observedGeneration":1,"currentHealthy":2,"desiredHealthy":2,"expectedPods":3,"disruptionsAllowed":0,"conditions":[{"type":"DisruptionAllowed","status":"False","reason":"InsufficientPods","observedGeneration":1}]}
        self.assertTrue(budget_blocks_second_peer(current,original))
        for change in (lambda p:p["status"].update(observedGeneration=0),lambda p:p["status"]["conditions"][0].update(reason="SyncFailed"),
                lambda p:p["status"]["conditions"][0].update(observedGeneration=0),lambda p:p["status"].update(expectedPods=1),
                lambda p:p["status"].update(disruptedPods={"another":"2026-01-01T00:00:00Z"}),lambda p:p["status"].update(disruptionsAllowed=1)):
            item=copy.deepcopy(current);change(item);self.assertFalse(budget_blocks_second_peer(item,original))
        for change in (lambda p:p["metadata"].update(uid="replacement"),lambda p:p["spec"].update(minAvailable=1)):
            item=copy.deepcopy(current);change(item)
            with self.assertRaises(ValueError):budget_blocks_second_peer(item,original)

    def test_recovery_requires_actual_post_resume_renewal_not_host_clock(self):
        original={"metadata":{"name":"worker","namespace":"kube-node-lease","uid":"original","resourceVersion":"10"},"spec":{"holderIdentity":"worker","renewTime":"2030-01-01T00:00:00Z"}}
        fence=copy.deepcopy(original)
        self.assertFalse(fresh_original_lease(original,original,fence))
        current=copy.deepcopy(original);current["metadata"]["resourceVersion"]="11"
        self.assertFalse(fresh_original_lease(current,original,fence))
        current["spec"]["renewTime"]="2030-01-01T00:00:01Z"
        self.assertTrue(fresh_original_lease(current,original,fence))
        for change in (lambda p:p["metadata"].update(uid="replacement"),lambda p:p["spec"].update(holderIdentity="other"),lambda p:p["spec"].update(renewTime="2030-01-01T00:00:01")):
            item=copy.deepcopy(current);change(item)
            with self.assertRaises(ValueError):fresh_original_lease(item,original,fence)

    def test_cleanup_attempts_every_original_guest_after_an_error(self):
        nodes=[{"name":n,"directory":Path(n)} for n in ("one","two","three")];paused=list(nodes)
        originals={n["name"]:{"pid":i} for i,n in enumerate(nodes)};attempted=[];restored=[]
        def identity(node):
            attempted.append(node["name"])
            return {"pid":999} if node["name"]=="two" else originals[node["name"]]
        def control(path,command):
            if path.parent.name=="three":raise OSError("synthetic lost QMP reply")
            return {"status":"running"}
        with self.assertRaises(RuntimeError):restore_original_guests(paused,originals,identity,control,lambda n:restored.append(n["name"]))
        self.assertEqual(attempted,["three","two","one"]);self.assertEqual(restored,["one"]);self.assertEqual([n["name"] for n in paused],["two","three"])
        restored.clear()
        restore_original_guests(paused,originals,lambda n:originals[n["name"]],lambda p,c:{"status":"running"},lambda n:restored.append(n["name"]))
        self.assertFalse(paused);self.assertEqual(restored,["three","two"])

    def test_bounded_probe_exceptions_retain_output_without_command_or_credentials(self):
        error=subprocess.TimeoutExpired(["synthetic","private-command-value"],1,output=b"response",stderr=b"deadline")
        result=bounded_failure_output(error,[])
        self.assertEqual(result,{"stdout":"response","stderr":"deadline"});self.assertNotIn("private-command-value",json.dumps(result))
        error=SubprocessOutputLimitExceeded(["synthetic"],8,"response","overflow")
        self.assertEqual(bounded_failure_output(error,[]),{"stdout":"response","stderr":"overflow"})
        with self.assertRaises(ValueError):bounded_failure_output(error,[b"response"])

    def test_only_linked_graceful_failed_node_controller_restart_can_qualify(self):
        previous={"containerID":"containerd://original","restartCount":0}
        current={"containerID":"containerd://recovered","restartCount":1,"ready":True,"state":{"running":{}},"lastState":{"terminated":{"containerID":"containerd://original","exitCode":0,"reason":"Completed"}}}
        log="leadership lost, stopped leading"
        self.assertTrue(graceful_failed_controller_transition(previous,current,1,0,log)["graceful_leadership_fencing_verified"])
        with self.assertRaises(ValueError):graceful_failed_controller_transition(previous,current,0,0,log)
        for change in (lambda p:p["lastState"]["terminated"].update(reason="OOMKilled",exitCode=137),lambda p:p["lastState"]["terminated"].update(containerID="unrelated"),lambda p:p.update(restartCount=2),lambda p:p.update(ready=False)):
            item=copy.deepcopy(current);change(item)
            with self.assertRaises(ValueError):graceful_failed_controller_transition(previous,item,1,0,log)
        for text in ("",log+" panic: unexpected failure"):
            with self.assertRaises(ValueError):graceful_failed_controller_transition(previous,current,1,0,text)

    def test_restart_credits_cannot_transfer_between_fault_intervals(self):
        anchor={"containerID":"original","restartCount":0}
        first={"containerID":"first","restartCount":1,"ready":True,"state":{"running":{}},"lastState":{"terminated":{"containerID":"original","reason":"Completed","exitCode":0}}}
        log="leadership lost, stopped leading"
        with self.assertRaises(ValueError):graceful_failed_controller_transition(anchor,first,2,0,log)
        self.assertIsNotNone(graceful_failed_controller_transition(anchor,first,1,0,log))
        second=copy.deepcopy(first);second.update(containerID="second",restartCount=2);second["lastState"]["terminated"]["containerID"]="first"
        with self.assertRaises(ValueError):graceful_failed_controller_transition(anchor,second,1,0,log)
        self.assertIsNotNone(graceful_failed_controller_transition(first,second,1,1,log))

    def test_unchanged_controller_identity_does_not_substitute_for_final_readiness(self):
        anchor={"containerID":"original","restartCount":0}
        for state in ({"ready":False,"state":{"running":{}}},{"ready":False,"state":{"terminated":{"exitCode":1}}}):
            with self.assertRaises(ValueError):graceful_failed_controller_transition(anchor,{**anchor,**state},0,0,"")
        self.assertIsNone(graceful_failed_controller_transition(anchor,{**anchor,"ready":True,"state":{"running":{}}},0,0,""))

    def test_default_is_local_and_claims_no_deployment_or_ha(self):
        with mock.patch.object(deploy,"Client") as client:
            status=deploy.operate(self.root,self.digest)
        client.assert_not_called();self.assertTrue(status["local_inputs_verified"]);self.assertFalse(status["deployed"])
        self.assertFalse(status["admission_verified"]);self.assertFalse(status["full_stack_verified"])

    def test_resources_replicas_disruption_and_separate_failure_domains_are_preserved(self):
        objects=json.loads(self.files["kyverno-components.json"])["items"]
        self.assertEqual(len(objects),73);self.assertFalse(any(o["kind"]=="Namespace" for o in objects))
        for obj in objects:
            if obj["kind"]!="Deployment":continue
            desired,cpu,memory,limit=renderer.CONTROLLERS[obj["metadata"]["name"]];spec=obj["spec"];pod=spec["template"]["spec"]
            self.assertEqual(spec["replicas"],desired);self.assertEqual(pod["containers"][0]["resources"],{"requests":{"cpu":cpu,"memory":memory},"limits":{"memory":limit}})
            self.assertEqual({t["topologyKey"] for t in pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]},{"kubernetes.io/hostname","topology.kubernetes.io/zone"})
            for container in [*pod.get("initContainers",[]),*pod["containers"]]:
                self.assertIn(container["image"].split('@')[1],renderer.IMAGES.values());self.assertNotIn(':v',container["image"])
                self.assertEqual(container["imagePullPolicy"],"IfNotPresent");self.assertIn({"name":"SSL_CERT_FILE","value":"/etc/platform/trust/ca.crt"},container["env"])
            pdb=next(p for p in objects if p["kind"]=="PodDisruptionBudget" and p["metadata"]["name"]==obj["metadata"]["name"])
            self.assertEqual(pdb["spec"]["minAvailable"],desired-1)
        init=next(o for o in objects if o["kind"]=="Deployment" and "initContainers" in o["spec"]["template"]["spec"])["spec"]["template"]["spec"]["initContainers"][0]
        self.assertEqual(init["resources"],{"requests":{"cpu":"100m","memory":"64Mi"}})

    def test_release_and_reviewed_package_drift_refuse_before_client(self):
        with self.assertRaises(ValueError):renderer.render(self.plan,self.release+b"\n")
        (self.root/"kyverno-components.json").write_text("{}")
        with mock.patch.object(deploy,"Client") as client,self.assertRaises(ValueError):deploy.operate(self.root,self.digest,apply=True)
        client.assert_not_called()

    def test_unknown_fields_and_unpinned_trust_are_not_accepted(self):
        for change in (lambda p:p.update(replicas=1),lambda p:p.update(schema_version=True),lambda p:p["trust_configmap"].update(ca_sha256=""),lambda p:p.update(namespace_uid="")):
            plan=copy.deepcopy(self.plan);change(plan)
            with self.assertRaises(ValueError):renderer.render(plan,self.release)

    def test_server_check_uses_server_side_dry_run_and_never_applies(self):
        client=FakeClient(self.plan)
        with mock.patch.object(deploy,"Client",return_value=client),mock.patch.object(deploy,"verify_target"):
            status=deploy.operate(self.root,self.digest,check_server=True)
        self.assertEqual(len(client.calls),1);args=client.calls[0][0]
        self.assertIn("--dry-run=server",args);self.assertIn("--server-side",args);self.assertNotIn("--force-conflicts",args)
        self.assertTrue(status["server_checked"]);self.assertFalse(status["deployed"])

    def test_namespace_and_immutable_ca_identity_changes_refuse_all_writes(self):
        for changed in ("namespace","trust-uid","mutable-ca","trust-bytes"):
            client=FakeClient(self.plan);client.changed=changed
            with mock.patch.object(deploy,"Client",return_value=client),mock.patch.object(deploy,"verify_target"),self.assertRaises(ValueError):
                deploy.operate(self.root,self.digest,apply=True)
            self.assertFalse(client.calls)

    def test_existing_foreign_objects_cannot_be_adopted(self):
        client=FakeClient(self.plan);client.foreign=True
        with mock.patch.object(deploy,"Client",return_value=client),mock.patch.object(deploy,"verify_target"),self.assertRaises(ValueError):
            deploy.operate(self.root,self.digest,apply=True)
        self.assertFalse(client.calls)

    def test_apply_waits_for_all_four_controller_roles_and_reports_original_uids(self):
        client=FakeClient(self.plan)
        with mock.patch.object(deploy,"Client",return_value=client),mock.patch.object(deploy,"verify_target"):
            status=deploy.operate(self.root,self.digest,apply=True)
        self.assertEqual(len(client.calls),2);self.assertIn("--dry-run=server",client.calls[0][0]);self.assertNotIn("--dry-run=server",client.calls[1][0])
        self.assertTrue(status["controller_deployments_ready"]);self.assertEqual(set(status["controller_uids"]),set(renderer.CONTROLLERS))
        self.assertFalse(status["admission_verified"]);self.assertFalse(status["ha_verified"])


if __name__=="__main__":unittest.main()
