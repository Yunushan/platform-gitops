#!/usr/bin/env python3
"""Bounded admission worker pauses, two-survivor enforcement and fail-closed recovery."""

import copy
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from bounded_subprocess import (
    SubprocessCaptureError,
    SubprocessOutputLimitExceeded,
    run_bounded,
)
from kyverno_admission_state import (
    budget_blocks_second_peer,
    fresh_original_lease,
    graceful_failed_controller_transition,
    ready_endpoint_uids,
)
from kyverno_policy_status import original_policy_ready
from strict_json import loads_strict_json
from test_k3s_vm_fixture import qmp
from test_kyverno_log_witness_fixture import (
    OriginalLogPool,
    previous_logs,
    select_previous_logs,
)


def bounded_failure_output(error,private_values):
    result={}
    for name in ("stdout","stderr"):
        value=getattr(error,name,"") or ""
        result[name]=value.decode("utf-8",errors="replace") if isinstance(value,bytes) else value
    if any(value in (result["stdout"]+result["stderr"]).encode() for value in private_values):raise ValueError("Private credential appeared in bounded failure output")
    return result


def restore_original_guests(paused,originals,identity,control,record):
    """Attempt every original guest even if one cleanup operation fails."""
    failures=[]
    for node in list(reversed(paused)):
        try:
            if identity(node)!=originals[node["name"]]:raise ValueError("Original guest identity changed during cleanup")
            state=control(node["directory"]/"qmp.sock","query-status")["status"]
            if state=="paused":control(node["directory"]/"qmp.sock","cont")
            elif state!="running":raise ValueError("Original guest cannot resume from this state")
            if control(node["directory"]/"qmp.sock","query-status")["status"]!="running":raise ValueError("Original guest did not resume")
            paused.remove(node);record(node)
        except (OSError,ValueError,RuntimeError,AssertionError,KeyError,TypeError) as error:failures.append(type(error).__name__)
    if failures:raise RuntimeError("Some original guests did not resume; all tracked guests were attempted")


def named_webhook_eof(row,hook,policy):
    text=row.get('stderr');output=row.get('stdout')
    if type(row.get('returncode')) is not int or row['returncode']!=1 or not isinstance(text,str) or not isinstance(output,str) or output.strip():return False
    lower=text.casefold()
    if any(value in lower for value in ('denied the request','x509','certificate','forbidden','unauthorized','invalid value','handshake')):return False
    post='Post "https://kyverno-svc.kyverno.svc:443/ivpol/validate/'+policy+'?timeout=10s": '
    return (text.startswith('Error from server (InternalError):') and 'failed calling webhook "'+hook+'"' in text
        and any(text.strip().endswith(post+value) for value in ('EOF','unexpected EOF')))


def converge_signed_dry_run(perform,ready,hook,policy,deadline,*,checkpoint=lambda:None,clock=time.monotonic,pause=time.sleep):
    # Only the caller's signed server-dry-run can enter this bounded transport
    # convergence gate. Every actual request remains in the probe ledger.
    for attempt in range(1,4):
        if clock()>=deadline:raise TimeoutError('Signed admission transport convergence deadline')
        if ready() is not True:raise ValueError('Original two-peer admission endpoint set changed')
        remaining=deadline-clock()
        if remaining<1:raise TimeoutError('Signed admission transport convergence deadline')
        row=perform(attempt,min(45,int(remaining)))
        row.update(transport_attempt=attempt,transport_deadline_monotonic=deadline)
        if row['returncode']==0:
            checkpoint()
            if clock()>deadline:raise TimeoutError('Late signed admission response cannot qualify convergence')
            return row
        row['named_webhook_transport_eof']=named_webhook_eof(row,hook,policy)
        retry=row['named_webhook_transport_eof'] and attempt<3 and clock()<deadline
        row['transport_retry']=retry;checkpoint()
        if not retry:return row
        pause(min(0.5,max(0,deadline-clock())))
    raise AssertionError('Unreachable admission attempt state')


def application_scope(baseline,policies,signed,unsigned):
    """Select only the original three-Pod workload and its enforcing policy."""
    workload,service=baseline['workload'],baseline['service']
    namespace=workload['metadata']['namespace'];selector=workload['spec']['selector']['matchLabels']
    if workload.get('kind')!='Deployment' or service.get('kind')!='Service' or not selector or workload['spec']['replicas']!=3:
        raise ValueError('Require the original three-replica Deployment and Service')
    if service['metadata']['namespace']!=namespace or service['spec']['selector']!=selector:
        raise ValueError('Original application Service scope differs')
    if signed['metadata']['namespace']!=namespace or unsigned['metadata']['namespace']!=namespace:
        raise ValueError('Admission probes must target the original application namespace')
    image=workload['spec']['template']['spec']['containers'][0]['image']
    if signed['spec']['containers'][0]['image']!=image or unsigned['spec']['containers'][0]['image']==image:
        raise ValueError('Admission probes must distinguish the original signed image')
    pods=baseline['pods'];uids={p['metadata']['uid'] for p in pods}
    if len(pods)!=3 or len(uids)!=3 or any(p['metadata']['namespace']!=namespace or
            any(p['metadata']['labels'].get(k)!=v for k,v in selector.items()) or p['spec']['containers'][0]['image']!=image for p in pods):
        raise ValueError('Original application Pod inventory differs')
    matching=[p for p in policies if p['spec']['matchConstraints'].get('namespaceSelector')=={'matchLabels':{'kubernetes.io/metadata.name':namespace}}]
    if len(matching)!=1 or matching[0]['spec'].get('failurePolicy')!='Fail' or matching[0]['spec'].get('validationActions')!=['Deny']:
        raise ValueError('Require one original fail-closed signature policy for the application namespace')
    policy=matching[0]
    result={'namespace':namespace,'namespace_uid':baseline['namespace_uids'][namespace],
        'deployment':workload['metadata']['name'],'deployment_uid':workload['metadata']['uid'],
        'service':service['metadata']['name'],'service_uid':service['metadata']['uid'],
        'selector':','.join(k+'='+v for k,v in sorted(selector.items())),
        'policy_name':policy['metadata']['name'],'policy_uid':policy['metadata']['uid'],
        'policy_generation':policy['metadata']['generation'],'pod_uids':sorted(uids)}
    if any(not result[k] for k in ('namespace_uid','deployment_uid','service_uid','policy_uid')):
        raise ValueError('Original workload and policy identities must be pinned')
    return result


def exercise(test,context,baseline,policies,signed,unsigned,serving,private_values):
    pool=OriginalLogPool(test,context,private_values);result=None
    try:
        result=_exercise(test,context,baseline,policies,signed,unsigned,serving,private_values,pool)
    finally:
        witnesses=pool.close()
        path=Path(os.environ['KYVERNO_ADMISSION_TEST_REPORT']).parent/'admission-failure-progress.json'
        if path.exists():
            progress=loads_strict_json(path.read_bytes());progress.update(original_log_witnesses=witnesses,log_witness_cleanup_verified=True)
            path.write_text(json.dumps(progress,sort_keys=True)+'\n',encoding='utf-8');path.chmod(0o600)
    result.update(original_log_witnesses=witnesses,log_witness_cleanup_verified=True)
    return result


def _exercise(test,context,baseline,policies,signed,unsigned,serving,private_values,pool):
    client=context["client"];workers=context["nodes"][1:]
    scope=application_scope(baseline,policies,signed,unsigned)
    test.assertEqual(len(workers),3)
    test.assertEqual({n["name"] for n in workers},{"fixture-worker-1","fixture-worker-2","fixture-worker-3"})
    original_service=client.get("service/kyverno-svc","kyverno")
    original_pods=baseline["controller_pods"]
    admission=[p for p in original_pods if p["metadata"]["labels"].get("app.kubernetes.io/component")=="admission-controller"]
    test.assertEqual(len(admission),3)
    all_uids={p["metadata"]["uid"] for p in admission}
    original_nodes={n["name"]:client.get("node/"+n["name"]) for n in workers}
    original_leases={n["name"]:client.get("lease.coordination.k8s.io/"+n["name"],"kube-node-lease") for n in workers}
    snapshots=[];probes=[];faults=[];paused=[];recovery_observations=[];controller_transitions=[];epoch_anchors={};validated_transitions=set();stage="setup";resumed_after=None;lease_fences={}
    def checkpoint():
        path=Path(os.environ["KYVERNO_ADMISSION_TEST_REPORT"]).parent/"admission-failure-progress.json"
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(descriptor,"w") as output:json.dump({"application_scope":scope,"stage":stage,"snapshots":snapshots,"probes":probes,"faults":faults,"paused_worker_names":[n["name"] for n in paused],"recovery_observations":recovery_observations,"controller_transitions":controller_transitions,"lease_fences":lease_fences,"resumed_after":resumed_after.isoformat() if resumed_after else None},output,sort_keys=True);output.write("\n")
    def identity(node):
        proc=Path("/proc")/str(node["qemu_pid"])
        return {"pid":node["qemu_pid"],"start_ticks":int((proc/"stat").read_text().rsplit(") ",1)[1].split()[19]),
            "boot_id":Path("/proc/sys/kernel/random/boot_id").read_text().strip(),"cgroup":(proc/"cgroup").read_text().strip()}
    originals={n["name"]:identity(n) for n in workers}
    last_seen={(pod["metadata"]["uid"],status["name"]):copy.deepcopy(status) for pod in original_pods for status in pod["status"]["containerStatuses"]}
    def pause(node):
        test.assertEqual(identity(node),originals[node["name"]]);test.assertEqual(qmp(node["directory"]/"qmp.sock","query-status")["status"],"running")
        epoch=len(faults);anchor_snapshot=snapshots[-1];anchors=[]
        for pod in anchor_snapshot["pods"]:
            if pod["spec"]["nodeName"]==node["name"]:
                for container in pod["status"]["containerStatuses"]:
                    entry={"pod_uid":pod["metadata"]["uid"],"container":container["name"],"status":copy.deepcopy(container)}
                    anchors.append(entry);epoch_anchors[(entry["pod_uid"],entry["container"])]=(epoch,entry["status"])
                    pool.start(pod,container,epoch)
        paused.append(node);checkpoint();qmp(node["directory"]/"qmp.sock","stop")
        test.assertEqual(qmp(node["directory"]/"qmp.sock","query-status")["status"],"paused")
        faults.append({"action":"pause","node":node["name"],"monotonic":time.monotonic(),"original_process":originals[node["name"]],"epoch":epoch,"anchor_snapshot_phase":anchor_snapshot["phase"],"container_anchors":anchors});checkpoint()
    def resume_all():
        nonlocal resumed_after,lease_fences
        def record(node):
            faults.append({"action":"resume","node":node["name"],"monotonic":time.monotonic(),"original_process":originals[node["name"]]})
            checkpoint()
        restore_original_guests(paused,originals,identity,qmp,record)
        if "lab_time" in context:context["lab_time"].synchronize(workers,context["ssh"],context["wait"])
        resumed_after=datetime.now(UTC)
        lease_fences={n["name"]:client.get("lease.coordination.k8s.io/"+n["name"],"kube-node-lease") for n in workers}
        checkpoint()
    def slices():
        return loads_strict_json(client.call(["get","endpointslices","-n","kyverno","-l","kubernetes.io/service-name=kyverno-svc","-o","json"]))["items"]
    def endpoints(expected):
        return ready_endpoint_uids(slices(),original_service,admission)==expected
    def normalized_hooks(rows):
        return {row["metadata"]["name"]:{"uid":row["metadata"]["uid"],"webhooks":sorted(row.get("webhooks",[]),key=lambda h:h["name"])} for row in rows}
    original_hooks=normalized_hooks(baseline["webhooks"])
    def collect_previous(entry,pod,current,*,wait=False):
        try:
            captured=previous_logs(client,pod,current['name'],private_values);entry['previous_api_read']=captured
            if captured['returncode']!=0:entry['previous_log_capture_failed']=True
        except (subprocess.TimeoutExpired,SubprocessOutputLimitExceeded,SubprocessCaptureError,OSError,ValueError) as error:
            entry['previous_log_capture_failed']=True;entry['capture_exception_type']=type(error).__name__;entry['capture_output']=bounded_failure_output(error,private_values)
        previous=current.get('lastState',{}).get('terminated',{}).get('containerID')
        witness=pool.witness(pod['metadata']['uid'],previous,wait=wait) if previous else None
        entry.update(select_previous_logs(entry.get('previous_api_read'),witness))

    def capture(label,expected):
        snapshot={"phase":label,"paused_worker_names":[n["name"] for n in paused],"monotonic":time.monotonic(),"recovery_heartbeat_after":resumed_after.isoformat() if resumed_after else None,"lease_fences":copy.deepcopy(lease_fences)}
        snapshots.append(snapshot);checkpoint()
        try:
            snapshot["service"]=client.get("service/kyverno-svc","kyverno")
            snapshot["controllers"]={name:client.get("deployment.apps/"+name,"kyverno") for name in baseline["controllers"]}
            snapshot["pods"]=loads_strict_json(client.call(["get","pods","-n","kyverno","-o","json"]))["items"]
            snapshot["slices"]=slices()
            snapshot["policies"]=[client.get("imagevalidatingpolicy.policies.kyverno.io/"+p["metadata"]["name"]) for p in policies]
            snapshot["webhooks"]=loads_strict_json(client.call(["get","validatingwebhookconfigurations","-o","json"]))["items"]
            snapshot["nodes"]={n["name"]:client.get("node/"+n["name"]) for n in workers}
            snapshot["leases"]={n["name"]:client.get("lease.coordination.k8s.io/"+n["name"],"kube-node-lease") for n in workers}
            snapshot["application"]=client.get("deployment.apps/"+scope["deployment"],scope["namespace"])
            snapshot["application_namespace"]=client.get("namespace/"+scope["namespace"])
            snapshot["application_service"]=client.get("service/"+scope["service"],scope["namespace"])
            snapshot["application_pods"]=loads_strict_json(client.call(["get","pods","-n",scope["namespace"],"-l",scope["selector"],"-o","json"]))["items"]
            # Collect previous logs for changed containers BEFORE validating;
            # a failed identity assertion must not discard its cause.
            changed=[];snapshot["changed_containers"]=changed
            for pod in snapshot["pods"]:
                old=next((p for p in original_pods if p["metadata"]["uid"]==pod["metadata"]["uid"]),None)
                if not old:continue
                original_status={c["name"]:c for c in old["status"]["containerStatuses"]}
                for container in pod.get("status",{}).get("containerStatuses",[]):
                    if container["name"] in original_status and container["containerID"]!=original_status[container["name"]]["containerID"]:
                        entry={"pod_uid":pod["metadata"]["uid"],"node":pod["spec"]["nodeName"],"container":container["name"],"status":container}
                        changed.append(entry)
                        if any(n["name"]==pod["spec"]["nodeName"] for n in paused):entry["log_deferred_while_guest_paused"]=True;continue
                        collect_previous(entry,pod,container,wait=True)
            snapshot["changed_containers"]=changed
        finally:checkpoint()
        service=snapshot["service"];test.assertEqual(service["metadata"]["uid"],original_service["metadata"]["uid"]);test.assertEqual(service["spec"],original_service["spec"])
        for name,item in snapshot["controllers"].items():
            test.assertEqual(item["metadata"]["uid"],baseline["controllers"][name]["metadata"]["uid"]);test.assertEqual(item["metadata"]["generation"],baseline["controllers"][name]["metadata"]["generation"]);test.assertEqual(item["spec"],baseline["controllers"][name]["spec"])
        test.assertEqual({p["metadata"]["uid"] for p in snapshot["pods"]},{p["metadata"]["uid"] for p in original_pods})
        for pod in snapshot["pods"]:
            old=next(p for p in original_pods if p["metadata"]["uid"]==pod["metadata"]["uid"])
            test.assertEqual(pod["spec"],old["spec"])
            if pod["spec"]["nodeName"] in {n["name"] for n in paused}:continue
            for current in pod["status"]["containerStatuses"]:
                key=(pod["metadata"]["uid"],current["name"]);anchor=epoch_anchors.get(key)
                previous=anchor[1] if anchor else last_seen[key]
                logs=next((e.get("previous_logs","") for e in snapshot.get("changed_containers",[]) if e["pod_uid"]==key[0] and e["container"]==key[1]),"")
                transition=graceful_failed_controller_transition(previous,current,1 if anchor else 0,previous["restartCount"],logs)
                if transition:
                    witness=(anchor[0],key[0],key[1],current["containerID"])
                    if witness not in validated_transitions:
                        controller_transitions.append({"pod_uid":key[0],"node":pod["spec"]["nodeName"],"container":key[1],"phase":label,"fault_epoch":anchor[0],**transition});validated_transitions.add(witness)
                    last_seen[key]=copy.deepcopy(current)
        advertised=ready_endpoint_uids(snapshot["slices"],service,admission);snapshot["ready_endpoint_uids"]=sorted(advertised);checkpoint()
        if expected is not None:test.assertEqual(advertised,expected)
        for current,original in zip(snapshot["policies"],policies,strict=True):test.assertTrue(original_policy_ready(current,original))
        test.assertEqual(normalized_hooks(snapshot["webhooks"]),original_hooks)
        for name,node in snapshot["nodes"].items():test.assertEqual(node["metadata"]["uid"],original_nodes[name]["metadata"]["uid"])
        test.assertEqual(snapshot["application_namespace"]["metadata"]["uid"],scope["namespace_uid"])
        for current,old in ((snapshot["application"],baseline["workload"]),(snapshot["application_service"],baseline["service"])):
            test.assertEqual(current["metadata"]["uid"],old["metadata"]["uid"]);test.assertEqual(current["spec"],old["spec"])
        test.assertEqual({p["metadata"]["uid"] for p in snapshot["application_pods"]},{p["metadata"]["uid"] for p in baseline["pods"]})
        for pod in snapshot["application_pods"]:
            old=next(p for p in baseline["pods"] if p["metadata"]["uid"]==pod["metadata"]["uid"])
            test.assertEqual(pod["spec"],old["spec"]);test.assertEqual([(c["containerID"],c["restartCount"]) for c in pod["status"]["containerStatuses"]],[(c["containerID"],c["restartCount"]) for c in old["status"]["containerStatuses"]])
        if label in {"single-worker-recovered","all-original-workers-recovered","final-after-recovery-probes"}:
            for name,item in snapshot["controllers"].items():
                test.assertEqual(item["status"].get("observedGeneration"),item["metadata"]["generation"])
                for field in ("replicas","readyReplicas","updatedReplicas","availableReplicas"):test.assertEqual(item["status"].get(field),baseline["controllers"][name]["spec"]["replicas"])
            for pod in snapshot["pods"]:
                test.assertTrue(any(c["type"]=="Ready" and c["status"]=="True" for c in pod["status"]["conditions"]))
                test.assertTrue(all(c["ready"] is True and "running" in c.get("state",{}) for c in pod["status"]["containerStatuses"]))
        return snapshot
    def probe(document,case,expected,*,raw=None,hook=None,transport_deadline=None,ready=None):
        command=["create","--raw",raw,"-f","-"] if raw else ["create","--dry-run=server","-f","-","-o","json"]
        def perform(attempt,request_seconds):
            try:result=run_bounded([*client.prefix,"--request-timeout="+str(request_seconds)+"s",*command],input=json.dumps(document),text=True,timeout=request_seconds+15,check=False,output_max_bytes=1024*1024)
            except (subprocess.TimeoutExpired,SubprocessOutputLimitExceeded,SubprocessCaptureError,OSError) as error:
                captured=bounded_failure_output(error,private_values);stdout,stderr=captured["stdout"],captured["stderr"]
                probes.append({"case":case,"expected":expected,"returncode":None,"exception_type":type(error).__name__,"document":document,"stdout":stdout,"stderr":stderr,"monotonic":time.monotonic(),"attempt":attempt});checkpoint()
                raise
            test.assertFalse(any(value in (result.stdout+result.stderr).encode() for value in private_values))
            row={"case":case,"expected":expected,"returncode":result.returncode,"document":document,"stdout":result.stdout,"stderr":result.stderr,"monotonic":time.monotonic(),"attempt":attempt}
            probes.append(row);checkpoint();return row
        if transport_deadline is not None:
            test.assertEqual((case,expected,raw),('two-survivors-signed','admit',None))
            row=converge_signed_dry_run(perform,ready,hook,scope['policy_name'],transport_deadline,checkpoint=checkpoint)
        else:row=perform(1,45)
        if expected=="admit":
            test.assertEqual(row['returncode'],0)
            returned=loads_strict_json(row['stdout']);test.assertEqual(returned["metadata"]["name"],document["metadata"]["name"])
            test.assertEqual(returned["spec"]["containers"][0]["image"],document["spec"]["containers"][0]["image"])
        else:
            test.assertNotEqual(row['returncode'],0)
            if expected=="signature-deny":
                test.assertIn("denied the request",row['stderr']);test.assertIn(scope["policy_name"],row['stderr']);test.assertIn("Release signature and signed bindings must verify.",row['stderr'])
            elif expected=="budget-deny":
                test.assertIn("TooManyRequests",row['stderr']);test.assertIn("disruption budget",row['stderr'])
            elif expected=="fail-closed":
                test.assertIn("failed calling webhook",row['stderr']);test.assertIn(hook,row['stderr'])
            else:raise AssertionError("Unknown failure probe")
        return row
    def named(document,name):
        result=copy.deepcopy(document);result["metadata"]["name"]=name;return result
    def recovered():
        observation={"stage":stage,"monotonic":time.monotonic(),"lease_fences":copy.deepcopy(lease_fences),"leases":{},"nodes":{},"pods":[],"controllers":{},"changed_containers":[]}
        recovery_observations.append(observation)
        if len(recovery_observations)>64:del recovery_observations[0]
        try:
            observation["slices"]=slices()
            observation["nodes"]={n["name"]:client.get("node/"+n["name"]) for n in workers}
            observation["leases"]={n["name"]:client.get("lease.coordination.k8s.io/"+n["name"],"kube-node-lease") for n in workers}
            rows=loads_strict_json(client.call(["get","pods","-n","kyverno","-o","json"]))["items"]
            observation["pods"]=[{"uid":row["metadata"]["uid"],"conditions":row.get("status",{}).get("conditions",[]),"containers":row.get("status",{}).get("containerStatuses",[])} for row in rows]
            observation["controllers"]={name:client.get("deployment.apps/"+name,"kyverno") for name in baseline["controllers"]}
            for pod in rows:
                for status in pod.get("status",{}).get("containerStatuses",[]):
                    old=last_seen.get((pod["metadata"]["uid"],status["name"]))
                    if old and status.get("containerID")!=old["containerID"]:
                        entry={"pod_uid":pod["metadata"]["uid"],"container":status["name"],"status":status};observation["changed_containers"].append(entry)
                        collect_previous(entry,pod,status)
            if ready_endpoint_uids(observation["slices"],original_service,admission)!=all_uids:return False
            for node in workers:
                if not any(c["type"]=="Ready" and c["status"]=="True" for c in observation["nodes"][node["name"]].get("status",{}).get("conditions",[])):return False
                if node["name"] not in lease_fences or not fresh_original_lease(observation["leases"][node["name"]],original_leases[node["name"]],lease_fences[node["name"]]):return False
            if len(rows)!=9 or not all(any(c["type"]=="Ready" and c["status"]=="True" for c in row.get("status",{}).get("conditions",[])) and all(c.get("ready") is True for c in row.get("status",{}).get("containerStatuses",[])) for row in rows):return False
            for name,old in baseline["controllers"].items():
                item=observation["controllers"][name];status=item.get("status",{})
                if status.get("observedGeneration")!=item["metadata"]["generation"] or any(status.get(k)!=old["spec"]["replicas"] for k in ("replicas","readyReplicas","updatedReplicas","availableReplicas")):return False
            observation["ready"]=True;return True
        finally:checkpoint()
    stage="baseline"
    try:
        context["wait"](lambda:endpoints(all_uids),seconds=30);capture("baseline",all_uids)
        stage="one-worker-paused";print(json.dumps({"kyverno_failure_stage":stage}),flush=True)
        victim=workers[-1];lost=next(p for p in admission if p["spec"]["nodeName"]==victim["name"]);survivors=all_uids-{lost["metadata"]["uid"]}
        started=time.monotonic();pause(victim)
        context["wait"](lambda:endpoints(survivors),seconds=100)
        convergence=time.monotonic()-started
        during=capture("two-original-survivors",survivors)
        test.assertFalse(any(c["type"]=="Ready" and c["status"]=="True" for c in during["nodes"][victim["name"]]["status"]["conditions"]))
        admission_hook=next(h["name"] for row in baseline["webhooks"] for h in row.get("webhooks",[]) if h.get("clientConfig",{}).get("service",{}).get("path")=="/ivpol/validate/"+scope["policy_name"])
        admission_deadline=faults[0]['monotonic']+100
        signed_result=probe(named(signed,"signed-two-survivors"),"two-survivors-signed","admit",hook=admission_hook,transport_deadline=admission_deadline,ready=lambda:endpoints(survivors))
        admission_convergence=signed_result['monotonic']-faults[0]['monotonic']
        probe(named(unsigned,"unsigned-two-survivors"),"two-survivors-unsigned","signature-deny")
        budget=None
        def budget_ready():
            nonlocal budget
            budget=client.get("poddisruptionbudget.policy/kyverno-admission-controller","kyverno")
            return budget_blocks_second_peer(budget,next(p for p in baseline["controller_disruption_budgets"] if p["metadata"]["name"]=="kyverno-admission-controller"))
        context["wait"](budget_ready,seconds=20)
        survivor=next(p for p in admission if p["metadata"]["uid"] in survivors)
        current_survivor=client.get("pod/"+survivor["metadata"]["name"],"kyverno")
        test.assertEqual(current_survivor["metadata"]["uid"],survivor["metadata"]["uid"])
        test.assertTrue(any(c["type"]=="Ready" and c["status"]=="True" for c in current_survivor["status"]["conditions"]))
        eviction={"apiVersion":"policy/v1","kind":"Eviction","metadata":{"name":survivor["metadata"]["name"],"namespace":"kyverno"},"deleteOptions":{"dryRun":["All"],"preconditions":{"uid":survivor["metadata"]["uid"]}}}
        probe(eviction,"second-peer-eviction","budget-deny",raw="/api/v1/namespaces/kyverno/pods/"+survivor["metadata"]["name"]+"/eviction?dryRun=All")
        budget_after=client.get("poddisruptionbudget.policy/kyverno-admission-controller","kyverno")
        test.assertTrue(budget_blocks_second_peer(budget_after,next(p for p in baseline["controller_disruption_budgets"] if p["metadata"]["name"]=="kyverno-admission-controller")))
        test.assertEqual(budget_after["metadata"]["resourceVersion"],budget["metadata"]["resourceVersion"])
        for node in workers[:-1]:context["wait"](lambda node=node:serving(node),seconds=15)
        test.assertEqual(qmp(victim["directory"]/"qmp.sock","query-status")["status"],"paused");capture("two-survivors-after-probes",survivors)
        resume_all();context["wait"](recovered,seconds=100);capture("single-worker-recovered",all_uids)
        stage="all-admission-workers-paused";print(json.dumps({"kyverno_failure_stage":stage}),flush=True)
        started=time.monotonic()
        for node in workers:pause(node)
        # A fail-closed metadata webhook may also block EndpointSlice/Node
        # updates during total loss. Original VM pause identities, not cached
        # endpoint readiness, establish that every real backend is unavailable.
        for node in workers:test.assertEqual(qmp(node["directory"]/"qmp.sock","query-status")["status"],"paused")
        capture("all-admission-workers-down",None)
        test.assertEqual(client.get("namespace/kube-system")["metadata"]["uid"],context["target"]["cluster_uid"])
        hook=next(h["name"] for row in baseline["webhooks"] for h in row.get("webhooks",[]) if h.get("clientConfig",{}).get("service",{}).get("path")=="/ivpol/validate/"+scope["policy_name"])
        probe(named(signed,"signed-all-admission-down"),"all-backends-down-signed","fail-closed",hook=hook)
        test.assertIsNone(client.get("pod/signed-all-admission-down",scope["namespace"],optional=True))
        capture("all-workers-down-after-refusal",None)
        total_outage_probe_seconds=time.monotonic()-started
    finally:resume_all()
    stage="original-workers-recovered";print(json.dumps({"kyverno_failure_stage":stage}),flush=True)
    context["wait"](recovered,seconds=100);capture("all-original-workers-recovered",all_uids)
    probe(named(signed,"signed-after-recovery"),"recovered-signed","admit")
    probe(named(unsigned,"unsigned-after-recovery"),"recovered-unsigned","signature-deny")
    for node in workers:context["wait"](lambda node=node:serving(node),seconds=30)
    resumed_after=datetime.now(UTC)
    lease_fences={n["name"]:client.get("lease.coordination.k8s.io/"+n["name"],"kube-node-lease") for n in workers}
    context["wait"](recovered,seconds=60);capture("final-after-recovery-probes",all_uids)
    return {"application_scope":scope,"snapshots":snapshots,"probes":probes,"faults":faults,"original_vm_processes":originals,"controller_transitions":controller_transitions,"original_worker_leases":original_leases,"recovery_observations":recovery_observations,"single_peer_endpoint_convergence_seconds":convergence,"single_peer_admission_convergence_seconds":admission_convergence,"single_peer_admission_deadline_seconds":100,"survivor_transport_eof_count":sum(p.get("named_webhook_transport_eof") is True for p in probes),"actual_admission_request_count":len(probes),"total_outage_probe_seconds":total_outage_probe_seconds,"endpoint_convergence_not_assumed_during_total_outage":True,
        "admission_service_uid":original_service["metadata"]["uid"],"single_peer_loss_after_convergence_verified":True,"second_peer_eviction_refused":True,"disruption_budget_during_peer_loss":budget,"disruption_budget_after_refusal":budget_after,
        "all_backends_down_fail_closed_verified":True,"original_identity_recovery_verified":True,"original_surviving_containers_preserved":True,"failed_node_graceful_controller_transitions_verified":bool(controller_transitions),"failed_node_graceful_transition_count":len(controller_transitions),"continuous_zero_error_availability_verified":False,"physical_ha_verified":False,"full_stack_verified":False}
