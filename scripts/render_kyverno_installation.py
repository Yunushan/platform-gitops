#!/usr/bin/env python3
"""Render an explicit pinned Kyverno HA installation with private CA trust."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from bounded_file import read_bounded_bytes
from kubernetes_management import inventory, validate_target
from render_gerrit_router import write_bundle
from strict_json import loads_strict_json
from strict_yaml import loads_strict_yaml_all

ROOT = Path(__file__).resolve().parents[1]
RELEASE_SHA = "4f785937dc7fe49a0731108b15def1538923c73d9236255bc69d94d498037969"
VERSION = "v1.19.2"
OWNER_LABEL = "platform.gitops/kyverno-installation"
IMAGES = {
 "reg.kyverno.io/kyverno/kyverno:v1.19.2": "sha256:6f0c520054d863a4a3df53ac7bb67d571d20421b77b99809013a62ec5f1d5947",
 "reg.kyverno.io/kyverno/kyvernopre:v1.19.2": "sha256:380c58effb6f065d4e856c084564482930eef789f47fab9dafa68a1964139a81",
 "reg.kyverno.io/kyverno/background-controller:v1.19.2": "sha256:7643ae5d759ae45f5c6035c62d9db4de6b325e513e36d43cfe47f3bc95dadac7",
 "reg.kyverno.io/kyverno/cleanup-controller:v1.19.2": "sha256:518233195e34e772a73277fafeaa7ff6a4920d5822c34be998e0f3b6b82494ce",
 "reg.kyverno.io/kyverno/reports-controller:v1.19.2": "sha256:9fbfa8cce7008bc45be75b2f6c4bf40e5ff582466fe571c1a38733a16a2bb510",
}
CONTROLLERS = {
 "kyverno-admission-controller": (3, "100m", "256Mi", "768Mi"),
 "kyverno-background-controller": (2, "100m", "256Mi", "768Mi"),
 "kyverno-cleanup-controller": (2, "50m", "128Mi", "512Mi"),
 "kyverno-reports-controller": (2, "100m", "256Mi", "768Mi"),
}


def canonical(value): return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()


def validate(plan):
    if not isinstance(plan,dict) or set(plan)!={"schema_version","runtime","management_target","namespace_uid","trust_configmap"} or type(plan["schema_version"]) is not int or plan["schema_version"]!=1:
        raise ValueError("Use the explicit Kyverno installation plan")
    if plan["runtime"] not in {"rke2","k3s","k8s","docker","raw"}: raise ValueError("Choose the control runtime")
    validate_target(plan["management_target"])
    uid = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
    if not isinstance(plan["namespace_uid"],str) or not re.fullmatch(uid,plan["namespace_uid"]): raise ValueError("Pin the existing kyverno namespace")
    trust=plan["trust_configmap"]
    if not isinstance(trust,dict) or set(trust)!={"name","uid","ca_sha256"}: raise ValueError("Pin the public private-CA ConfigMap")
    if not isinstance(trust["name"],str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*",trust["name"]) or len(trust["name"])>63: raise ValueError("Use an explicit CA ConfigMap name")
    if not isinstance(trust["uid"],str) or not re.fullmatch(uid,trust["uid"]): raise ValueError("Pin the original CA ConfigMap UID")
    if not isinstance(trust["ca_sha256"],str) or not re.fullmatch(r"[0-9a-f]{64}",trust["ca_sha256"]): raise ValueError("Pin the reviewed CA certificate bytes")


def render(plan,release):
    validate(plan)
    if hashlib.sha256(release).hexdigest()!=RELEASE_SHA: raise ValueError("Require the reviewed official Kyverno release")
    objects=list(loads_strict_yaml_all(release.decode()))
    if len(objects)!=70: raise ValueError("Unexpected original release inventory")
    deployments={o["metadata"]["name"] for o in objects if o["kind"]=="Deployment"}
    if deployments!=set(CONTROLLERS): raise ValueError("Require all four controller roles")
    owner=hashlib.sha256(canonical(plan)).hexdigest()[:32]
    result=[];seen=set()
    for obj in objects:
        if obj["kind"]=="Namespace":
            if obj["metadata"]["name"]!="kyverno": raise ValueError("Unexpected upstream namespace")
            continue
        obj["metadata"].setdefault("labels",{})[OWNER_LABEL]=owner
        if obj["kind"]=="Deployment":
            name=obj["metadata"]["name"];replicas,cpu,memory,limit=CONTROLLERS[name]
            spec=obj["spec"];spec["replicas"]=replicas;spec["strategy"]={"type":"RollingUpdate","rollingUpdate":{"maxSurge":0,"maxUnavailable":1}}
            selector=spec["selector"]["matchLabels"];pod=spec["template"]["spec"]
            pod["nodeSelector"]={"kubernetes.io/os":"linux"}
            pod["affinity"]={"podAntiAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":[{"labelSelector":{"matchLabels":selector},"topologyKey":key} for key in ("kubernetes.io/hostname","topology.kubernetes.io/zone")]}}
            pod.setdefault("volumes",[]).append({"name":"platform-private-ca","configMap":{"name":plan["trust_configmap"]["name"],"items":[{"key":"ca.crt","path":"ca.crt"}]}})
            spec["template"]["metadata"].setdefault("annotations",{})["platform.gitops/private-ca-sha256"]=plan["trust_configmap"]["ca_sha256"]
            for c in [*pod.get("initContainers",[]),*pod["containers"]]:
                tag=c["image"]
                if tag not in IMAGES: raise ValueError("Unexpected controller image")
                seen.add(tag);c["image"]=tag.rsplit(":",1)[0]+"@"+IMAGES[tag];c["imagePullPolicy"]="IfNotPresent"
                c.setdefault("env",[]).extend([{"name":"SSL_CERT_FILE","value":"/etc/platform/trust/ca.crt"},{"name":"SSL_CERT_DIR","value":"/etc/platform/trust"}])
                c.setdefault("volumeMounts",[]).append({"name":"platform-private-ca","mountPath":"/etc/platform/trust","readOnly":True})
            if len(pod["containers"])!=1: raise ValueError("Unexpected controller container inventory")
            pod["containers"][0]["resources"]={"requests":{"cpu":cpu,"memory":memory},"limits":{"memory":limit}}
            result.append({"apiVersion":"policy/v1","kind":"PodDisruptionBudget","metadata":{"name":name,"namespace":"kyverno","labels":{OWNER_LABEL:owner}},"spec":{"minAvailable":replicas-1,"selector":{"matchLabels":selector}}})
        result.append(obj)
    if seen!=set(IMAGES): raise ValueError("Incomplete reviewed controller images")
    payload=canonical({"apiVersion":"v1","kind":"List","items":result})+b"\n"
    contract={"schema_version":1,"plan":plan,"release_sha256":RELEASE_SHA,"version":VERSION,"owner":owner,
        "inventory":inventory(result),"components_sha256":hashlib.sha256(payload).hexdigest()}
    text=canonical(contract).decode()+"\n"
    return {"kyverno-components.json":payload.decode(),"kyverno-installation-contract.json":text,
        "kyverno-installation-status.json":json.dumps({"contract_sha256":hashlib.sha256(text.encode()).hexdigest(),"prepared":True,"deployed":False,"admission_verified":False,"ha_verified":False,"full_stack_verified":False})+"\n"}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan",type=Path,required=True);parser.add_argument("--release",type=Path,required=True);parser.add_argument("--output-dir",type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        plan=loads_strict_json(read_bounded_bytes(args.plan,max_bytes=128*1024));release=read_bounded_bytes(args.release,max_bytes=8*1024*1024)
        write_bundle(render(plan,release),args.output_dir,private_root=ROOT/"private")
        print(json.dumps({"prepared":True,"deployed":False,"admission_verified":False,"full_stack_verified":False}));return 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        print("Kyverno preparation stopped; private plans and targets suppressed.",file=sys.stderr);return 1


if __name__=="__main__":raise SystemExit(main())
