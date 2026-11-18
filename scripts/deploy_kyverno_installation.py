#!/usr/bin/env python3
"""Explicitly install a reviewed Kyverno HA package; default is local inspection."""

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import render_kyverno_installation as renderer
from bounded_file import read_bounded_bytes
from kubernetes_management import Client, inventory, verify_owned, verify_target
from strict_json import loads_strict_json


def load(root,digest):
    raw=read_bounded_bytes(root/"kyverno-installation-contract.json",max_bytes=256*1024)
    if not isinstance(digest,str) or not re.fullmatch(r"[0-9a-f]{64}",digest) or hashlib.sha256(raw).hexdigest()!=digest:raise ValueError("Reviewed package changed")
    contract=loads_strict_json(raw)
    if not isinstance(contract,dict) or set(contract)!={"schema_version","plan","release_sha256","version","owner","inventory","components_sha256"} or type(contract["schema_version"]) is not int or contract["schema_version"]!=1:raise ValueError("Invalid package")
    renderer.validate(contract["plan"])
    if contract["release_sha256"]!=renderer.RELEASE_SHA or contract["version"]!=renderer.VERSION or contract["owner"]!=hashlib.sha256(renderer.canonical(contract["plan"])).hexdigest()[:32]:raise ValueError("Unexpected installation identity")
    payload=read_bounded_bytes(root/"kyverno-components.json",max_bytes=12*1024*1024)
    if hashlib.sha256(payload).hexdigest()!=contract["components_sha256"]:raise ValueError("Reviewed components changed")
    document=loads_strict_json(payload)
    if set(document)!={"apiVersion","kind","items"} or document["kind"]!="List" or len(document["items"])!=73 or inventory(document["items"])!=contract["inventory"]:raise ValueError("Installation inventory changed")
    return contract,payload


def inputs(client,plan):
    verify_target(client,plan["management_target"])
    namespace=client.get("namespace/kyverno")
    if not namespace or namespace["metadata"]["uid"]!=plan["namespace_uid"] or namespace["metadata"].get("deletionTimestamp"):raise ValueError("Original namespace changed")
    trust=plan["trust_configmap"];obj=client.get("configmap/"+trust["name"],"kyverno")
    if (not obj or obj["metadata"]["uid"]!=trust["uid"] or obj.get("immutable") is not True or set(obj.get("data",{}))!={"ca.crt"}
            or hashlib.sha256(obj["data"]["ca.crt"].encode()).hexdigest()!=trust["ca_sha256"]):raise ValueError("Original public trust changed")


def ready(client):
    observed={}
    for name,(desired,_,_,_) in renderer.CONTROLLERS.items():
        obj=client.get("deployment.apps/"+name,"kyverno");status=obj.get("status",{});generation=obj["metadata"].get("generation",0)
        if obj["spec"].get("replicas")!=desired or status.get("observedGeneration",0)<generation or any(status.get(key)!=desired for key in ("replicas","readyReplicas","updatedReplicas","availableReplicas")):return None
        observed[name]=obj["metadata"]["uid"]
    return observed


def operate(root,digest,*,apply=False,check_server=False):
    contract,payload=load(root,digest);plan=contract["plan"]
    if not apply and not check_server:return {"local_inputs_verified":True,"deployed":False,"admission_verified":False,"full_stack_verified":False}
    client=Client(plan["management_target"])
    with client.frozen():
        inputs(client,plan)
        for item in contract["inventory"]:verify_owned(client,item,renderer.OWNER_LABEL,contract["owner"])
        arguments=["apply","--server-side","--field-manager=platform-kyverno","-f","-"]
        client.call([*arguments,"--dry-run=server"],payload=payload,seconds=90)
        if not apply:return {"server_checked":True,"deployed":False,"admission_verified":False,"full_stack_verified":False}
        inputs(client,plan);client.call(arguments,payload=payload,seconds=120)
        deadline=time.monotonic()+300
        while True:
            inputs(client,plan);observed=ready(client)
            if observed:return {"deployed":True,"controller_deployments_ready":True,"controller_uids":observed,"admission_verified":False,"ha_verified":False,"full_stack_verified":False}
            if time.monotonic()>=deadline:raise ValueError("Original controllers did not become ready")
            time.sleep(2)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir",type=Path,required=True);parser.add_argument("--contract-sha256",required=True)
    mode=parser.add_mutually_exclusive_group();mode.add_argument("--apply",action="store_true");mode.add_argument("--check-server",action="store_true")
    args=parser.parse_args(argv)
    try:print(json.dumps(operate(args.bundle_dir,args.contract_sha256,apply=args.apply,check_server=args.check_server),sort_keys=True));return 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):print("Kyverno installation stopped; targets and private diagnostics suppressed.",file=sys.stderr);return 1


if __name__=="__main__":raise SystemExit(main())
