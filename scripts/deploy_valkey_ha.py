#!/usr/bin/env python3
"""Create a fresh pinned Valkey/Sentinel Kubernetes scope; observe original handles."""

import argparse
import copy
from datetime import datetime,timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

from atomic_file import atomic_write_text
from bootstrap_keycloak_realm import sources
from bounded_file import read_bounded_bytes
import deploy_openbao_ha as infrastructure
from kubernetes_management import Client
import render_valkey_ha as renderer
from strict_json import loads_strict_json


ROOT=Path(__file__).resolve().parents[1]
OPERATION="platform.gitops/valkey-operation"
CONTRACT="platform.gitops/valkey-contract"
UNVERIFIED=("tls_verified","authentication_verified","replication_verified","sentinel_quorum_verified","failover_verified","application_compatibility_verified",
            "lossless_failover_verified","storage_encryption_verified","network_datapath_verified","off_platform_restore_verified","physical_node_loss_verified","ha_verified","full_stack_verified")
canonical=infrastructure.canonical
fingerprint=infrastructure.fingerprint
uid=infrastructure.uid
sha=infrastructure.sha
includes=infrastructure.includes
condition=infrastructure.condition
controlled=infrastructure.controlled
items=infrastructure.items
key=infrastructure.key


def toolset(): return sources(Path(__file__).stem)


def load_package(bundle,digest):
    if not sha(digest): raise ValueError("Pin the exact Valkey package")
    raw=read_bounded_bytes(bundle/"valkey-contract.json",max_bytes=256*1024)
    if hashlib.sha256(raw).hexdigest()!=digest: raise ValueError("The original Valkey contract changed")
    contract=loads_strict_json(raw); renderer.exact(contract,"schema_version runtime plan files_sha256")
    if type(contract["schema_version"]) is not int or contract["schema_version"]!=1 or contract["runtime"] not in {"rke2","k3s","k8s"}: raise ValueError("Select a Kubernetes Valkey package")
    files=renderer.render(contract["plan"],contract["runtime"])
    if files["valkey-contract.json"].encode()!=raw: raise ValueError("The package differs from current rendering policy")
    for name,value in files.items():
        if read_bounded_bytes(bundle/name,max_bytes=2*1024**2)!=value.encode(): raise ValueError("An original artifact changed")
    order=("NetworkPolicy","ConfigMap","Service","PodDisruptionBudget","Deployment")
    return contract,sorted(renderer.kubernetes(contract["plan"]),key=lambda obj:order.index(obj["kind"]))


def infrastructure_plan(plan):
    # Reuse the existing target/node/Ceph/Cilium contract, with TLS metadata
    # only. Valkey auth Secrets are separately checked without reading values.
    return {**{k:v for k,v in plan.items() if k not in {"tls_secrets","auth_secrets"}},"secrets":plan["tls_secrets"]}


def validate(plan,contract):
    renderer.exact(plan,"schema_version contract_sha256 target namespace_uid node_uids tls_secrets auth_secrets claims storage_classes cilium admitted_sha256")
    selected=copy.deepcopy(contract["plan"])
    for member in selected["members"]:
        member.update(data_claim=member["server_claim"],audit_claim=member["sentinel_claim"])
    infrastructure.validate(infrastructure_plan(plan),{"plan":selected})
    expected={m["auth_secret"] for m in selected["members"]}
    if (not isinstance(plan["auth_secrets"],dict) or set(plan["auth_secrets"])!=expected or any(not uid(v) for v in plan["auth_secrets"].values())
            or len(set(plan["auth_secrets"].values()))!=3 or set(plan["tls_secrets"]) & expected or len(set([*plan["tls_secrets"].values(),*plan["auth_secrets"].values()]))!=6):
        raise ValueError("Pin six distinct immutable TLS/auth Secret identities")


def prerequisites(client,plan,selected):
    infrastructure.prerequisites(client,infrastructure_plan(plan),selected)
    template='{{.metadata.uid}}{{"\\n"}}{{.type}}{{"\\n"}}{{.immutable}}{{"\\n"}}{{if .metadata.deletionTimestamp}}true{{else}}false{{end}}{{"\\n"}}'+''.join(
        '{{if index .data '+json.dumps(k)+'}}true{{else}}false{{end}}{{"\\n"}}' for k in ("server-auth.conf","server.acl","sentinel-auth.conf","sentinel.acl"))
    for name,identity in plan["auth_secrets"].items():
        rows=client.call(["get","secret/"+name,"--namespace",selected["namespace"],"-o","go-template="+template]).splitlines()
        if rows!=[identity,"Opaque","true","false","true","true","true","true"]: raise ValueError("An original immutable auth Secret or key-presence contract changed")
    for member in selected["members"]:
        for kind,minimum in (("server",selected["settings"]["maxmemory_mib"]*4*1024**2),("sentinel",64*1024**2)):
            claim=client.get("persistentvolumeclaim/"+member[kind+"_claim"],selected["namespace"])
            if infrastructure.quantity(claim["spec"]["resources"]["requests"]["storage"])<minimum: raise ValueError("A claim is below the reviewed cache/AOF or Sentinel sizing floor")


def safe_pod(pod,*,allow_node=False):
    if any(pod.get(k) for k in ("hostPID","hostIPC","hostNetwork","shareProcessNamespace","ephemeralContainers","runtimeClassName","dnsConfig","hostAliases","priorityClassName")): return False
    if not allow_node and pod.get("nodeName") or pod.get("schedulerName") not in {None,"default-scheduler"}: return False
    if pod.get("serviceAccountName") not in {None,"default"} or pod.get("automountServiceAccountToken") is not False: return False
    context=pod.get("securityContext",{})
    if context.get("sysctls") or context.get("supplementalGroups") or context.get("runAsUser")!=1000 or context.get("runAsGroup")!=1000 or context.get("fsGroup")!=1000 or context.get("runAsNonRoot") is not True or context.get("seccompProfile")!={"type":"RuntimeDefault"}: return False
    containers=pod.get("containers",[]); init=pod.get("initContainers",[])
    if len(containers)!=2 or {c.get("name") for c in containers}!={"server","sentinel"} or len(init)!=1 or init[0].get("name")!="seed-original-config": return False
    for container in [*containers,*init]:
        if any(container.get(k) for k in ("envFrom","args","lifecycle","stdin","stdinOnce","tty","workingDir","volumeDevices","restartPolicy")): return False
        if container in init:
            if container.get("env")!=[{"name":"VALKEY_INSTALL_OPERATION_ID","valueFrom":{"fieldRef":{"apiVersion":"v1","fieldPath":"metadata.annotations['platform.gitops/valkey-operation']"}}}]: return False
        elif container.get("env"): return False
        security=container.get("securityContext",{})
        if (security.get("privileged") or security.get("allowPrivilegeEscalation") is not False or security.get("readOnlyRootFilesystem") is not True or security.get("capabilities")!={"drop":["ALL"]}
                or security.get("procMount") not in {None,"Default"} or security.get("runAsUser",1000)!=1000 or security.get("runAsGroup",1000)!=1000 or security.get("runAsNonRoot") is False
                or security.get("seccompProfile",{"type":"RuntimeDefault"})!={"type":"RuntimeDefault"}): return False
        for name in ("startupProbe","readinessProbe","livenessProbe"):
            probe=container.get(name,{})
            if container in init:
                if probe: return False
            elif [k for k in ("exec","httpGet","tcpSocket","grpc") if k in probe]!=["tcpSocket"] or probe["tcpSocket"].get("host"): return False
    return True


def material(obj):
    meta=obj["metadata"]
    result={"apiVersion":obj["apiVersion"],"kind":obj["kind"],"metadata":{"name":meta["name"],"namespace":meta["namespace"],"labels":meta.get("labels",{}),
        "annotations":{k:v for k,v in meta.get("annotations",{}).items() if k not in {OPERATION,CONTRACT} and not (obj["kind"]=="Deployment" and k=="deployment.kubernetes.io/revision")}}}
    if obj["kind"]=="ConfigMap": result.update(data=obj.get("data",{}),immutable=obj.get("immutable"))
    else:
        spec=copy.deepcopy(obj["spec"])
        if obj["kind"]=="Deployment":
            metadata=spec["template"]["metadata"]
            metadata["annotations"]={k:v for k,v in metadata.get("annotations",{}).items() if k!=OPERATION}
        result["spec"]=spec
    return result


def admitted(expected,actual):
    if (not isinstance(actual,dict) or actual.get("apiVersion")!=expected["apiVersion"] or actual.get("kind")!=expected["kind"] or not includes(expected["metadata"],actual.get("metadata",{}))
            or actual["metadata"].get("ownerReferences") or actual["metadata"].get("finalizers") or actual["metadata"].get("deletionTimestamp")): raise ValueError("Admission changed reviewed ownership")
    if actual["kind"]=="ConfigMap":
        if actual.get("data")!=expected["data"] or actual.get("immutable") is not True or actual.get("binaryData"): raise ValueError("Keep exact immutable seed configuration")
    elif not includes(expected["spec"],actual.get("spec",{})): raise ValueError("Admission changed the original workload policy")
    if actual["kind"]=="Service":
        spec=actual["spec"]
        if (spec.get("type","ClusterIP")!="ClusterIP" or spec.get("clusterIP")!="None" or spec.get("clusterIPs",["None"])!=["None"] or spec.get("externalIPs") or spec.get("externalName")
                or spec.get("sessionAffinity","None")!="None" or spec.get("internalTrafficPolicy","Cluster")!="Cluster" or any(p.get("nodePort") for p in spec["ports"])): raise ValueError("Keep per-member headless internal discovery services")
    if actual["kind"]=="Deployment":
        template=actual["spec"]["template"]
        expected_annotations=expected["spec"]["template"]["metadata"].get("annotations",{})
        if (not re.fullmatch(r"[0-9a-f]{32}",str(expected_annotations.get(OPERATION,""))) or set(expected_annotations)!={OPERATION,CONTRACT}
                or actual["spec"].get("paused") or template.get("metadata",{}).get("annotations")!=expected_annotations or not safe_pod(template["spec"])
                or actual["metadata"].get("annotations",{}).get("deployment.kubernetes.io/revision") not in {None,"1"}): raise ValueError("Admission added an executable, privilege, annotation or rollout path")
    return fingerprint(material(actual))


def owned(expected,current,entry,state):
    if (not current or not uid(current.get("metadata",{}).get("uid")) or current["metadata"].get("annotations",{}).get(OPERATION)!=state["operation_id"]
            or current["metadata"].get("annotations",{}).get(CONTRACT)!=state["contract_sha256"] or entry["uid"] is not None and current["metadata"]["uid"]!=entry["uid"]
            or admitted(expected,current)!=entry["admitted_sha256"]): raise ValueError("An original resource was replaced or changed")
    return current


def fresh_scope(client,selected,*,member=None):
    wanted=renderer.LABELS if member is None else {**renderer.LABELS,"platform.gitops/member":member}
    claims={m[k] for m in selected["members"] if member is None or m["name"]==member for k in ("server_claim","sentinel_claim")}
    for pod in items(client,"pods",selected["namespace"]):
        if any(v.get("persistentVolumeClaim",{}).get("claimName") in claims for v in pod.get("spec",{}).get("volumes",[])): raise ValueError("A reviewed claim is already used by another Pod")
    for resource in ("deployments","replicasets","pods","services") if member is None else ("deployments","replicasets","pods"):
        for obj in items(client,resource,selected["namespace"]):
            spec=obj.get("spec",{})
            if any(includes(wanted,labels) for labels in (obj.get("metadata",{}).get("labels",{}),spec.get("selector",{}),spec.get("selector",{}).get("matchLabels",{}),spec.get("template",{}).get("metadata",{}).get("labels",{}))): raise ValueError("Existing Valkey resources require original recovery scope")


def status_handle(status,expected,*,init=False):
    if status.get("name")!=expected["name"] or status.get("image")!=expected["image"] or type(status.get("restartCount")) is not int or status["restartCount"]<0: return None
    container=status.get("containerID"); image=status.get("imageID")
    if (not isinstance(container,str) or not re.fullmatch(r"(?:containerd|cri-o|docker)://[0-9a-f]{64}",container) or not isinstance(image,str)
            or not re.fullmatch(r"(?:[a-z0-9-]+://)?(?:[A-Za-z0-9./:_-]+@)?sha256:[0-9a-f]{64}",image)): return None
    state=status.get("state",{}).get("terminated" if init else "running",{})
    if init:
        if type(state.get("exitCode")) is not int or state["exitCode"]!=0: return None
    elif status.get("started") is not True or status.get("ready") is not True: return None
    instant=state.get("finishedAt" if init else "startedAt")
    if not isinstance(instant,str) or len(instant)>40: return None
    try:
        parsed=datetime.fromisoformat(instant.replace("Z","+00:00"))
        if parsed.tzinfo is None or parsed.year<2020: return None
    except ValueError: return None
    return {"container_id":container,"image_id":image,"restart_count":status["restartCount"],"finished_at" if init else "started_at":instant}


def observe_started(client,selected,current):
    namespace=selected["namespace"]; selector=",".join(k+"="+v for k,v in renderer.LABELS.items())
    replicasets=items(client,"replicasets",namespace,selector); pods=items(client,"pods",namespace,selector)
    if len(replicasets)!=3 or len(pods)!=3: return None
    result={}
    for member in selected["members"]:
        deployment=current["deployment/"+member["name"]]; meta,status=deployment["metadata"],deployment.get("status",{})
        if type(meta.get("generation")) is not int or status.get("observedGeneration")!=meta["generation"] or any(status.get(k)!=1 for k in ("replicas","updatedReplicas","readyReplicas","availableReplicas")): return None
        found=[r for r in replicasets if controlled(r,"Deployment",meta["uid"])]
        if len(found)!=1: return None
        rs=found[0]; rm,spec=rs["metadata"],rs.get("spec",{})
        if (not uid(rm.get("uid")) or rm.get("deletionTimestamp") or spec.get("replicas")!=1 or type(rm.get("generation")) is not int or rs.get("status",{}).get("observedGeneration")!=rm["generation"]
                or any(rs.get("status",{}).get(k)!=1 for k in ("replicas","readyReplicas","availableReplicas")) or not includes(deployment["spec"]["template"],spec.get("template",{}))): return None
        found=[p for p in pods if controlled(p,"ReplicaSet",rm["uid"])]
        if len(found)!=1: return None
        pod=found[0]; pm,ps,state=pod["metadata"],pod.get("spec",{}),pod.get("status",{})
        template_meta=deployment["spec"]["template"]["metadata"]
        if (not infrastructure.mechanics.pod_ready(pod) or not condition(pod,"PodScheduled") or ps.get("nodeName")!=member["node"] or not includes({k:template_meta.get(k,{}) for k in ("labels","annotations")},pm)
                or not includes(deployment["spec"]["template"]["spec"],ps) or not safe_pod(ps,allow_node=True)): return None
        containers=state.get("containerStatuses",[]); init=state.get("initContainerStatuses",[])
        if len(containers)!=2 or len(init)!=1 or {c.get("name") for c in containers}!={"server","sentinel"}: return None
        handles={c["name"]:status_handle(c,next(e for e in ps["containers"] if e["name"]==c["name"])) for c in containers}
        seed=status_handle(init[0],ps["initContainers"][0],init=True)
        if seed is None or any(v is None for v in handles.values()): return None
        try: address=ipaddress.ip_address(state.get("podIP",""))
        except ValueError: return None
        if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast: return None
        result[member["name"]]={"deployment_uid":meta["uid"],"replicaset_uid":rm["uid"],"pod_uid":pm["uid"],"node":member["node"],"pod_ip":str(address),"containers":handles,"completed_seed":seed}
    if len({v["pod_ip"] for v in result.values()})!=3: return None
    return result


def operate(bundle,plan,*,mode="local",output=None,wait_seconds=180,client=None):
    contract,objects=load_package(bundle,plan["contract_sha256"]); validate(plan,contract); tools=toolset()
    result={"local_inputs_verified":True,"server_checked":False,"resources_observed":False,"member_processes_observed":False,"completed_seed_observed":False,
        "requires_runtime_verification":True,"read_only":True,**{k:False for k in UNVERIFIED}}
    if mode=="local": return result
    if mode not in {"check-server","apply","resume"} or output is None or type(wait_seconds) is not int or not 5<=wait_seconds<=1800: raise ValueError("Select a bounded explicit server operation")
    if mode!="check-server" and plan["admitted_sha256"] is None: raise ValueError("Review and pin server admission before creation")
    directory=output.resolve(); relative=directory.relative_to((ROOT/"private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use an original private installation directory")
    if mode!="resume": directory.mkdir(mode=0o700,parents=True,exist_ok=False)
    if not directory.is_dir(): raise ValueError("Resume requires its original journal")
    receipt=directory/"valkey-install-proof.json"; selected=contract["plan"]; namespace=selected["namespace"]
    with infrastructure.mechanics.lock(directory):
        if mode=="resume":
            if receipt.is_symlink(): raise ValueError("The original journal cannot redirect")
            state=loads_strict_json(read_bounded_bytes(receipt,max_bytes=256*1024))
            if (type(state.get("schema_version")) is not int or state["schema_version"]!=1 or state.get("action")!="apply" or state.get("plan_sha256")!=fingerprint(plan)
                    or state.get("contract_sha256")!=plan["contract_sha256"] or state.get("toolset_sha256")!=tools or not isinstance(state.get("operation_id"),str) or not re.fullmatch(r"[0-9a-f]{32}",state["operation_id"])
                    or not isinstance(state.get("objects"),dict) or set(state["objects"])!={key(o) for o in objects} or any(state.get(k) is not False for k in UNVERIFIED)): raise ValueError("Resume only the original matching installation")
            for entry in state["objects"].values():
                renderer.exact(entry,"attempted uid admitted_sha256")
                if type(entry["attempted"]) is not bool or entry["uid"] is not None and (not uid(entry["uid"]) or not entry["attempted"]) or entry["admitted_sha256"] is not None and not sha(entry["admitted_sha256"]): raise ValueError("Invalid original resource handle")
        else:
            state={"schema_version":1,"action":mode,"operation_id":uuid4().hex,"plan_sha256":fingerprint(plan),"contract_sha256":plan["contract_sha256"],"toolset_sha256":tools,
                "objects":{key(o):{"attempted":False,"uid":None,"admitted_sha256":None} for o in objects},**result}
        def record(phase,**values):
            state.update(phase=phase,recorded_at=datetime.now(timezone.utc).isoformat(),**values); atomic_write_text(receipt,canonical(state))
        record("local-validated",**result)
        for obj in objects:
            obj["metadata"]["annotations"]={OPERATION:state["operation_id"],CONTRACT:plan["contract_sha256"]}
            if obj["kind"]=="Deployment": obj["spec"]["template"]["metadata"]["annotations"]={OPERATION:state["operation_id"],CONTRACT:plan["contract_sha256"]}
        wrote=False
        try:
            network=client or Client(plan["target"])
            with network.frozen():
                prerequisites(network,plan,selected)
                if not any(e["attempted"] for e in state["objects"].values()):
                    fresh_scope(network,selected)
                    for obj in objects:
                        if network.get(key(obj),namespace,optional=True): raise ValueError("A target resource already exists")
                        response=loads_strict_json(network.call(["create","--dry-run=server","-f","-","-o","json"],payload=canonical(obj).encode(),seconds=60))
                        state["objects"][key(obj)]["admitted_sha256"]=admitted(obj,response)
                        atomic_write_text(directory/(key(obj).replace("/","-")+"-admitted.json"),canonical(material(response)))
                    aggregate=fingerprint({k:v["admitted_sha256"] for k,v in state["objects"].items()}); record("admission-reviewed",server_checked=True,admitted_sha256=aggregate)
                    if plan["admitted_sha256"] is not None and aggregate!=plan["admitted_sha256"]: raise ValueError("Server admission changed from reviewed material")
                    if mode=="check-server": return {**result,"server_checked":True,"admitted_sha256":aggregate}
                if fingerprint({k:v["admitted_sha256"] for k,v in state["objects"].items()})!=plan["admitted_sha256"]: raise ValueError("The original admission commitment changed")
                for obj in objects:
                    entry=state["objects"][key(obj)]; current=network.get(key(obj),namespace,optional=True)
                    if not entry["attempted"]:
                        if current: raise ValueError("A foreign resource appeared")
                        prerequisites(network,plan,selected)
                        if obj["kind"]=="Deployment": fresh_scope(network,selected,member=obj["metadata"]["name"])
                        if toolset()!=tools: raise ValueError("Installer source changed")
                        response=loads_strict_json(network.call(["create","--dry-run=server","-f","-","-o","json"],payload=canonical(obj).encode(),seconds=60))
                        if admitted(obj,response)!=entry["admitted_sha256"]: raise ValueError("Admission changed before creation")
                        entry["attempted"]=True; record("before-"+key(obj),read_only=False); wrote=True
                        current=loads_strict_json(network.call(["create","-f","-","-o","json"],payload=canonical(obj).encode(),seconds=60))
                        if uid(current.get("metadata",{}).get("uid")): entry["uid"]=current["metadata"]["uid"]; record("create-response-observed")
                    deadline=time.monotonic()+wait_seconds
                    while not current:
                        if entry["uid"] is not None: raise ValueError("An original resource is absent; never recreate it")
                        if time.monotonic()>=deadline: raise TimeoutError("Unknown create outcome; observe the original operation later")
                        time.sleep(1); current=network.get(key(obj),namespace,optional=True)
                    owned(obj,current,entry,state); entry["uid"]=current["metadata"]["uid"]; record("observed-"+key(obj))
                deadline=time.monotonic()+wait_seconds
                while True:
                    current={key(o):owned(o,network.get(key(o),namespace,optional=True),state["objects"][key(o)],state) for o in objects}; snapshot=observe_started(network,selected,current)
                    if snapshot:
                        if state.get("observed_members") is not None and state["observed_members"]!=snapshot: raise ValueError("An original Pod, seed or process invocation changed")
                        prerequisites(network,plan,selected)
                        final={key(o):owned(o,network.get(key(o),namespace,optional=True),state["objects"][key(o)],state) for o in objects}
                        if observe_started(network,selected,final)!=snapshot or toolset()!=tools: raise ValueError("The final original observation or source changed")
                        result.update(server_checked=True,resources_observed=True,member_processes_observed=True,completed_seed_observed=True,read_only=not wrote)
                        record("awaiting-runtime-verification",observed_members=snapshot,**result); return result
                    if time.monotonic()>=deadline: raise TimeoutError("Original containers or seed completion have not converged; preserve the journal")
                    record("waiting-for-original-workloads",server_checked=True,resources_observed=True); time.sleep(2)
        except BaseException:
            record("failed",failed_at=state["phase"],error_code="valkey-installation-unverified"); raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--bundle-dir",type=Path,required=True); parser.add_argument("--deployment-plan",type=Path,required=True); parser.add_argument("--output-dir",type=Path); parser.add_argument("--wait-seconds",type=int,default=180)
    modes=parser.add_mutually_exclusive_group()
    for flag in ("check-server","apply","resume"): modes.add_argument("--"+flag,action="store_true")
    args=parser.parse_args(argv)
    try:
        plan=loads_strict_json(read_bounded_bytes(args.deployment_plan,max_bytes=128*1024)); mode=next((v.replace("_","-") for v in ("check_server","apply","resume") if getattr(args,v)),"local")
        print(canonical(operate(args.bundle_dir,plan,mode=mode,output=args.output_dir,wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("Valkey installation stopped; retain the private journal and original handles. Details suppressed.",file=sys.stderr); return 1


if __name__=="__main__": raise SystemExit(main())
