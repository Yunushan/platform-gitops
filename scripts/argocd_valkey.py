#!/usr/bin/env python3
"""Prepare an optional dedicated Argo CD Sentinel/mTLS client; never apply it."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import yaml

from bounded_file import read_bounded_bytes
import materialize_valkey_auth as auth
import render_valkey_ha as renderer
from render_gerrit_router import write_bundle
from strict_json import loads_strict_json
import verify_valkey_runtime as observer


ROOT=Path(__file__).resolve().parents[1]
ROLES=(*auth.ROLES,"argocd_data","argocd_sentinel")
COMPONENTS=("argocd-server","argocd-repo-server","argocd-application-controller")
# All keys/channels belong to this application-exclusive cache group. No admin,
# scripting, topology mutation, flush, replication or arbitrary CONFIG access.
DATA=("+auth","+hello","+ping","+select","+get","+set","+del","+unlink","+rename","+scan","+publish","+subscribe","+unsubscribe","+client|setname","+client|setinfo")
SENTINEL=("+auth","+hello","+ping","+client|setname","+client|setinfo","+subscribe","+unsubscribe","+sentinel|get-master-addr-by-name","+sentinel|sentinels","+sentinel|replicas")
CLIENT_FILES={"auth":"argocd-auth","auth_username":"argocd-auth_username","sentinel_auth":"argocd-sentinel_auth","sentinel_username":"argocd-sentinel_username"}
FALSE_FLAGS=("deployed","argocd_server_revocation_cache_verified","argocd_server_cache_compatibility_verified","argocd_controller_cache_verified","argocd_repo_server_cache_verified","application_compatibility_verified","lossless_failover_verified","physical_node_loss_verified","ha_verified","full_stack_verified")


def validate(plan,contract):
    renderer.exact(plan,"schema_version cache_contract_sha256 application")
    if type(plan["schema_version"]) is not int or plan["schema_version"]!=1 or not observer.sha(plan["cache_contract_sha256"]): raise ValueError("Pin the original cache contract")
    app=plan["application"]; renderer.exact(app,"kind version namespace private_dir credential_secret tls_secret")
    if app["kind"]!="argocd" or app["version"]!="3.5.0" or any(not renderer.label(app[k]) for k in ("namespace","credential_secret","tls_secret")) or app["credential_secret"]==app["tls_secret"]: raise ValueError("Review Argo CD 3.5.0 and separate explicit private Secret references")
    path=app["private_dir"]
    if not isinstance(path,str) or not renderer.PATH.fullmatch(path) or any(v in {".",".."} for v in path.split("/")): raise ValueError("Use a canonical absolute private application mount")
    selected=contract["plan"]
    if selected["network"]["client_namespaces"]!=[app["namespace"]]: raise ValueError("Require an application-exclusive cache group and client namespace")
    paths=[selected["private_dir"],selected["binary"],*[m[k+"_dir"] for m in selected["members"] for k in ("server","sentinel")]]
    if any(path==p or path.startswith(p+"/") or p.startswith(path+"/") for p in paths): raise ValueError("Keep client inputs and cache member inputs/state disjoint")


def selected(bundle,plan):
    contract=observer.package(bundle,plan.get("cache_contract_sha256")); validate(plan,contract); return contract


def arguments(contract,plan):
    app=plan["application"]; base=app["private_dir"]+"/tls"; cache=contract["plan"]
    return ["--sentinelmaster="+cache["cache_name"],*["--sentinel="+renderer.address(cache,m,contract["runtime"])+":26380" for m in cache["members"]],
        "--redisdb=0","--redis-use-tls=true","--redis-insecure-skip-tls-verify=false","--redis-ca-certificate="+base+"/ca.crt","--redis-client-certificate="+base+"/tls.crt","--redis-client-key="+base+"/tls.key"]


def environment(plan):
    path=plan["application"]["private_dir"]+"/credentials"
    return {"REDIS_CREDS_DIR_PATH":path,"REPOSERVER_REDIS_CREDS_DIR_PATH":path}


def render(contract,plan):
    validate(plan,contract); app=plan["application"]; args=arguments(contract,plan); env=environment(plan)
    mounts=[{"name":"platform-argocd-cache-credentials","mountPath":app["private_dir"]+"/credentials","readOnly":True},{"name":"platform-argocd-cache-tls","mountPath":app["private_dir"]+"/tls","readOnly":True}]
    fragment={"containers":[{"name":name,"args":args,"env":[{"name":k,"value":v} for k,v in env.items()],"volumeMounts":mounts} for name in COMPONENTS],
        "volumes":[{"name":"platform-argocd-cache-credentials","secret":{"secretName":app["credential_secret"],"defaultMode":0o440,"items":[{"key":k,"path":k} for k in CLIENT_FILES]}},
            {"name":"platform-argocd-cache-tls","secret":{"secretName":app["tls_secret"],"defaultMode":0o440,"items":[{"key":k,"path":k} for k in ("ca.crt","tls.crt","tls.key")]}}]}
    return {"argocd-valkey-arguments.json":json.dumps(args,indent=2)+"\n","argocd-valkey-environment.json":json.dumps(env,sort_keys=True,indent=2)+"\n",
        "argocd-valkey-pod-fragment.yaml":yaml.safe_dump(fragment,sort_keys=False),"argocd-valkey-client-files.json":json.dumps(CLIENT_FILES,sort_keys=True,indent=2)+"\n",
        "argocd-valkey-status.json":json.dumps({"runtime":contract["runtime"],"application":"argocd","application_version":app["version"],"dedicated_cache_required":True,"prepared":True,**{k:False for k in FALSE_FLAGS}},sort_keys=True,indent=2)+"\n"}


def private_files(contract,plan,member,refs,*,environment=None):
    validate(plan,contract); renderer.exact(refs," ".join(ROLES))
    if any(not observer.reference(v) for v in refs.values()) or len(set(refs.values()))!=len(ROLES): raise ValueError("Reference distinct private credentials for every existing and application role")
    values=os.environ if environment is None else environment; chosen={role:values.get(refs[role],"") for role in ROLES}
    if any(not isinstance(v,str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}",v) for v in chosen.values()) or len(set(chosen.values()))!=len(ROLES): raise ValueError("Supply seven distinct high-entropy private role values")
    files=auth.files(contract["plan"],contract["runtime"],member,{k:refs[k] for k in auth.ROLES},environment=values)
    def acl(user,value,rights): return "user "+user+" reset on #"+hashlib.sha256(value.encode()).hexdigest()+" "+rights+"\n"
    files["server.acl"]+=acl("argocd_cache",chosen["argocd_data"],"allkeys allchannels -@all "+" ".join(DATA))
    files["sentinel.acl"]+=acl("argocd_discovery",chosen["argocd_sentinel"],"resetchannels &+switch-master &+replica-reconf-done -@all "+" ".join(SENTINEL))
    files.update({"argocd-auth":chosen["argocd_data"]+"\n","argocd-auth_username":"argocd_cache\n","argocd-sentinel_auth":chosen["argocd_sentinel"]+"\n","argocd-sentinel_username":"argocd_discovery\n"})
    return files


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--bundle-dir",type=Path,required=True); parser.add_argument("--application-plan",type=Path,required=True); parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--materialize",action="store_true"); parser.add_argument("--credential-refs",type=Path); parser.add_argument("--member"); args=parser.parse_args(argv)
    try:
        plan=loads_strict_json(read_bounded_bytes(args.application_plan,max_bytes=128*1024)); contract=selected(args.bundle_dir,plan)
        if args.materialize:
            if args.credential_refs is None or args.member is None: raise ValueError("Select original credential references and one member explicitly")
            refs=loads_strict_json(read_bounded_bytes(args.credential_refs,max_bytes=16*1024)); files=private_files(contract,plan,args.member,refs)
        else:
            if args.credential_refs is not None or args.member is not None: raise ValueError("Private inputs require explicit materialization mode")
            files=render(contract,plan)
        write_bundle(files,args.output_dir,private_root=ROOT/"private")
        print(json.dumps({"prepared":True,"materialized":args.materialize,**{k:False for k in FALSE_FLAGS}},sort_keys=True)); return 0
    except Exception:
        print("Argo CD cache preparation failed; private inputs and API details suppressed.",file=sys.stderr); return 1


if __name__=="__main__": raise SystemExit(main())
