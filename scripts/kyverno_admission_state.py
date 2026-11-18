#!/usr/bin/env python3
"""Resolve ready admission endpoints only through the original Service and Pods."""


from datetime import datetime


def ready_endpoint_uids(slices, service, original_pods):
    metadata=service["metadata"];name=metadata["name"];namespace=metadata["namespace"]
    pods={p["metadata"]["uid"]:p for p in original_pods}
    if len(pods)!=len(original_pods) or not pods:raise ValueError("Require distinct original admission Pods")
    result=set()
    for item in slices:
        m=item["metadata"];owners=m.get("ownerReferences",[])
        if (m.get("namespace")!=namespace or m.get("labels",{}).get("kubernetes.io/service-name")!=name
                or len(owners)!=1 or owners[0].get("kind")!="Service" or owners[0].get("name")!=name
                or owners[0].get("uid")!=metadata["uid"] or owners[0].get("controller") is not True or item.get("addressType")!="IPv4"):
            raise ValueError("Admission endpoint ownership changed")
        for endpoint in item.get("endpoints",[]):
            ref=endpoint.get("targetRef",{});pod=pods.get(ref.get("uid"))
            if (not pod or ref.get("kind")!="Pod" or ref.get("namespace")!=namespace or ref.get("name")!=pod["metadata"]["name"]
                    or endpoint.get("nodeName")!=pod["spec"]["nodeName"] or endpoint.get("addresses")!=[pod["status"]["podIP"]]):
                raise ValueError("Original admission endpoint identity changed")
            ports={c["name"]:(c["containerPort"],c.get("protocol","TCP")) for container in pod["spec"]["containers"] for c in container.get("ports",[])}
            expected={(p["name"],*ports[p["targetPort"]]) for p in service["spec"]["ports"]}
            actual={(p["name"],p["port"],p.get("protocol","TCP")) for p in item.get("ports",[])}
            if actual!=expected:raise ValueError("Admission endpoint port changed")
            readiness=endpoint.get("conditions",{}).get("ready")
            if type(readiness) is not bool:raise ValueError("Ambiguous endpoint readiness cannot prove convergence")
            if readiness:
                if endpoint["conditions"].get("terminating") is True or endpoint["conditions"].get("serving") is False:
                    raise ValueError("A terminating or non-serving endpoint is marked ready")
                if ref["uid"] in result:raise ValueError("Duplicate ready admission endpoint")
                result.add(ref["uid"])
    return result


def budget_blocks_second_peer(current,original):
    for field in ("name","namespace","uid","generation"):
        if current.get("metadata",{}).get(field)!=original["metadata"][field]:raise ValueError("Original disruption budget identity changed")
    if current.get("spec")!=original["spec"] or current["spec"].get("minAvailable")!=2:raise ValueError("Original disruption budget specification changed")
    status=current.get("status",{});generation=current["metadata"]["generation"]
    return (status.get("observedGeneration")==generation and status.get("currentHealthy")==2 and status.get("desiredHealthy")==2
        and status.get("expectedPods")==3 and status.get("disruptionsAllowed")==0 and not status.get("disruptedPods")
        and any(c.get("type")=="DisruptionAllowed" and c.get("status")=="False" and c.get("reason")=="InsufficientPods"
            and c.get("observedGeneration")==generation for c in status.get("conditions",[])))


def fresh_original_lease(current,original,fence):
    for item in (current,fence):
        for field in ("name","namespace","uid"):
            if item.get("metadata",{}).get(field)!=original["metadata"][field]:raise ValueError("Original worker Lease identity changed")
        if item.get("spec",{}).get("holderIdentity")!=original["spec"]["holderIdentity"]:raise ValueError("Original worker Lease holder changed")
    values=[]
    for item in (current,fence):
        text=item.get("spec",{}).get("renewTime")
        if not isinstance(text,str):return False
        value=datetime.fromisoformat(text.replace("Z","+00:00"))
        if value.tzinfo is None:raise ValueError("Require explicit UTC worker heartbeat times")
        values.append(value)
    # Both timestamps come from the same original worker clock. A changed API
    # resource version alone or an unchanged future timestamp is insufficient.
    return (bool(current["metadata"].get("resourceVersion")) and current["metadata"]["resourceVersion"]!=fence["metadata"].get("resourceVersion") and values[0]>values[1])


def graceful_failed_controller_transition(previous,current,pause_count,baseline_restarts,logs):
    if type(pause_count) is not int or pause_count not in (0,1) or baseline_restarts!=previous["restartCount"]:
        raise ValueError("Use only the current fault's original container anchor")
    if current.get("ready") is not True or "running" not in current.get("state",{}) or current.get("state",{}).get("terminated"):
        raise ValueError("Active controller container is not ready and running")
    if current["containerID"]==previous["containerID"]:
        if current["restartCount"]!=previous["restartCount"]:raise ValueError("Container restart counter changed without identity transition")
        return None
    terminated=current.get("lastState",{}).get("terminated",{})
    if (pause_count<1 or current.get("restartCount")!=previous["restartCount"]+1 or current["restartCount"]>baseline_restarts+pause_count
            or terminated.get("containerID")!=previous["containerID"] or terminated.get("reason")!="Completed" or terminated.get("exitCode")!=0
            or terminated.get("signal",0)!=0 or current.get("ready") is not True or "running" not in current.get("state",{})):
        raise ValueError("Unexplained controller restart or surviving-controller identity loss")
    text=logs.casefold()
    if "leadership lost, stopped leading" not in text or any(marker in text for marker in ("panic:","fatal error:","oomkilled","out of memory")):
        raise ValueError("Graceful failed-node controller fencing was not verified")
    return {"previous_container_id":previous["containerID"],"container_id":current["containerID"],"restart_count":current["restartCount"],"termination":terminated,"graceful_leadership_fencing_verified":True}
