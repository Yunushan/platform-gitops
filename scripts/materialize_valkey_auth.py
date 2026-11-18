#!/usr/bin/env python3
"""Materialize private scoped Valkey/Sentinel authentication files from env refs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from bounded_file import read_bounded_text
from render_gerrit_router import RUNTIMES, write_bundle
import render_valkey_ha as renderer
from strict_json import loads_strict_json


ROOT=Path(__file__).resolve().parents[1]
ROLES=("replication","sentinel_server","sentinel_peer","discovery","probe")
SERVER_CONTROL=("+ping","+info","+role","+multi","+exec","+subscribe","+publish","+slaveof","+replicaof","+config|rewrite","+client|setname","+client|kill","+script|kill")
DISCOVERY=("+auth","+ping","+hello","+command","+role","+info","+client|getname","+client|id","+client|setname","+sentinel|get-master-addr-by-name","+sentinel|master","+sentinel|myid","+sentinel|replicas","+sentinel|sentinels","+sentinel|ckquorum")


def validate_refs(refs):
    renderer.exact(refs," ".join(ROLES))
    if any(not isinstance(v,str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}",v) for v in refs.values()) or len(set(refs.values()))!=len(ROLES): raise ValueError("Use separate private environment references for every role")


def files(plan,runtime,name,refs,*,environment=None):
    renderer.validate(plan); validate_refs(refs)
    if runtime not in RUNTIMES or name not in {m["name"] for m in plan["members"]}: raise ValueError("Select one reviewed member/runtime")
    environment=os.environ if environment is None else environment; values={role:environment.get(refs[role],"") for role in ROLES}
    if any(not isinstance(v,str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}",v) for v in values.values()) or len(set(values.values()))!=len(ROLES): raise ValueError("Supply distinct high-entropy URL-safe credentials through the declared private environment")
    def user(role,rights): return "user "+role+" reset on #"+hashlib.sha256(values[role].encode()).hexdigest()+" "+rights+"\n"
    chosen=next(m for m in plan["members"] if m["name"]==name); private,_,_=renderer.locations(plan,chosen,runtime)
    server_acl="user default reset off\n"+user("replication","resetchannels -@all +psync +replconf +ping")
    server_acl+=user("sentinel_server","&__sentinel__:hello -@all "+" ".join(SERVER_CONTROL))
    server_acl+=user("probe","~platform-probe:* resetchannels -@all +ping +get +set +del +expire +ttl +wait +role +info")
    sentinel_acl="user default reset off\n"+user("sentinel_peer","allchannels allkeys +@all")+user("discovery","resetchannels -@all "+" ".join(DISCOVERY))
    quote=json.dumps; cache=plan["cache_name"]
    return {"server.acl":server_acl,"sentinel.acl":sentinel_acl,
        "server-auth.conf":"aclfile "+private+"/auth/server.acl\nmasteruser replication\nmasterauth "+quote(values["replication"])+"\n",
        "sentinel-auth.conf":"aclfile "+private+"/auth/sentinel.acl\nsentinel auth-user "+cache+" sentinel_server\nsentinel auth-pass "+cache+" "+quote(values["sentinel_server"])+
            "\nsentinel sentinel-user sentinel_peer\nsentinel sentinel-pass "+quote(values["sentinel_peer"])+"\n"}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--plan",type=Path,required=True); parser.add_argument("--runtime",choices=RUNTIMES,required=True)
    parser.add_argument("--member",required=True); parser.add_argument("--credential-refs",type=Path,required=True); parser.add_argument("--output-dir",type=Path,required=True); args=parser.parse_args(argv)
    try:
        plan=loads_strict_json(read_bounded_text(args.plan,max_bytes=128*1024)); refs=loads_strict_json(read_bounded_text(args.credential_refs,max_bytes=16*1024))
        result=files(plan,args.runtime,args.member,refs); write_bundle(result,args.output_dir,private_root=ROOT/"private")
        print('{"materialized":true,"files":4,"deployed":false,"application_compatibility_verified":false}'); return 0
    except Exception:
        print("Valkey authentication materialization stopped; private inputs suppressed.",file=sys.stderr); return 1


if __name__=="__main__": raise SystemExit(main())
