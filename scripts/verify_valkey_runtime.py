#!/usr/bin/env python3
"""Verify pinned TLS cache/Sentinel state, with an optional expiring write canary."""

import argparse
from datetime import datetime,timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import sys
import tempfile
import time
from uuid import uuid4

from atomic_file import atomic_write_text
from bootstrap_keycloak_realm import sources
from bounded_file import read_bootstrap_bytes,read_bounded_bytes
import render_valkey_ha as renderer
from strict_json import loads_strict_json
from valkey_protocol import Connection,ReplyError


FALSE_FLAGS=("application_compatibility_verified","lossless_failover_verified","physical_node_loss_verified","failover_verified","storage_durability_verified","plaintext_disabled_verified","ha_verified","full_stack_verified")


def canonical(value): return json.dumps(value,sort_keys=True,separators=(",",":"))
def fingerprint(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def sha(value): return isinstance(value,str) and bool(re.fullmatch(r"[0-9a-f]{64}",value))
def identifier(value): return isinstance(value,str) and bool(re.fullmatch(r"[0-9a-f]{40}",value))
def reference(value): return isinstance(value,str) and bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}",value))
def number(value):
    if not isinstance(value,str) or not re.fullmatch(r"0|[1-9][0-9]{0,18}",value): raise ValueError("Invalid numeric cache observation")
    return int(value)


def package(bundle,digest):
    if not sha(digest): raise ValueError("Pin the original cache package")
    raw=read_bounded_bytes(bundle/"valkey-contract.json",max_bytes=256*1024)
    if hashlib.sha256(raw).hexdigest()!=digest: raise ValueError("The original cache contract changed")
    contract=loads_strict_json(raw); renderer.exact(contract,"schema_version runtime plan files_sha256")
    if type(contract["schema_version"]) is not int or contract["schema_version"]!=1 or contract["runtime"] not in renderer.RUNTIMES: raise ValueError("Unsupported cache package")
    for name,value in renderer.render(contract["plan"],contract["runtime"]).items():
        if read_bounded_bytes(bundle/name,max_bytes=2*1024**2)!=value.encode(): raise ValueError("The original package differs from reviewed rendering policy")
    return contract


def validate(plan,contract):
    renderer.exact(plan,"schema_version contract_sha256 trust credentials members limits")
    if type(plan["schema_version"]) is not int or plan["schema_version"]!=1 or not sha(plan["contract_sha256"]): raise ValueError("Unsupported observation scope")
    trust=plan["trust"]; renderer.exact(trust,"ca_file ca_sha256 client_cert_file client_cert_sha256 client_key_file client_key_sha256")
    for kind in ("ca","client_cert","client_key"):
        if not isinstance(trust[kind+"_file"],str) or not Path(trust[kind+"_file"]).is_absolute() or not sha(trust[kind+"_sha256"]): raise ValueError("Pin absolute runner-local TLS inputs by byte hash")
    if len({trust[k+"_file"] for k in ("ca","client_cert","client_key")})!=3: raise ValueError("Keep TLS inputs separate")
    refs=plan["credentials"]; renderer.exact(refs,"probe_env discovery_env")
    if not all(reference(v) for v in refs.values()) or refs["probe_env"]==refs["discovery_env"]: raise ValueError("Reference separate scoped private credentials")
    members=plan["members"]; selected=contract["plan"]
    if not isinstance(members,dict) or set(members)!={m["name"] for m in selected["members"]}: raise ValueError("Pin all original direct member identities")
    all_addresses=[]
    for member in selected["members"]:
        value=members[member["name"]]; renderer.exact(value,"connect_address server_run_id sentinel_id server_cert_sha256 addresses")
        ip=ipaddress.IPv4Address(value["connect_address"])
        if str(ip)!=value["connect_address"] or ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_multicast or ip.is_reserved: raise ValueError("Select a specific direct member IPv4 address")
        addresses=value["addresses"]
        if (not identifier(value["server_run_id"]) or not identifier(value["sentinel_id"]) or not sha(value["server_cert_sha256"]) or not isinstance(addresses,list) or not 1<=len(addresses)<=4
                or not all(isinstance(v,str) and renderer.dns_name(v) for v in addresses) or len(set(addresses))!=len(addresses)
                or value["connect_address"] not in addresses or renderer.address(selected,member,contract["runtime"]) not in addresses): raise ValueError("Pin direct addresses, advertised aliases, run IDs, Sentinel IDs and TLS leaves")
        all_addresses.extend(addresses)
    for field in ("connect_address","server_run_id","sentinel_id","server_cert_sha256"):
        if len({m[field] for m in members.values()})!=3: raise ValueError("Member identities cannot alias")
    if len(set(all_addresses))!=len(all_addresses): raise ValueError("Advertised aliases must identify exactly one approved member")
    limits=plan["limits"]; renderer.exact(limits,"request_seconds operation_seconds max_response_bytes canary_ttl_seconds replication_wait_ms")
    bounds={"request_seconds":(2,30),"operation_seconds":(20,300),"max_response_bytes":(65536,1048576),"canary_ttl_seconds":(60,600),"replication_wait_ms":(1000,10000)}
    if any(type(limits[k]) is not int or not low<=limits[k]<=high for k,(low,high) in bounds.items()) or limits["canary_ttl_seconds"]<limits["operation_seconds"]+10 or limits["request_seconds"]*1000<=limits["replication_wait_ms"]: raise ValueError("Bound every request, complete operation, response, replication wait and canary lifetime")


class Network:
    def __init__(self,plan,contract):
        self.plan=plan; self.contract=contract; self.deadline=time.monotonic()+plan["limits"]["operation_seconds"]; trust=plan["trust"]; material={}
        for kind in ("ca","client_cert","client_key"):
            path=Path(trust[kind+"_file"]); info=path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or kind=="client_key" and os.name=="posix" and (info.st_uid not in {0,os.geteuid()} or stat.S_IMODE(info.st_mode) not in {0o400,0o440,0o600,0o640})): raise ValueError("Keep reviewed TLS inputs as original private regular files")
            raw=read_bootstrap_bytes(path,max_bytes=256*1024,follow_symlinks=False)
            if hashlib.sha256(raw).hexdigest()!=trust[kind+"_sha256"]: raise ValueError("A pinned TLS input changed")
            material[kind]=raw
        self.context=ssl.create_default_context(cadata=material["ca"].decode("ascii")); self.bare_context=ssl.create_default_context(cadata=material["ca"].decode("ascii"))
        for ctx in (self.context,self.bare_context): ctx.minimum_version=ssl.TLSVersion.TLSv1_2
        with tempfile.TemporaryDirectory(prefix="platform-valkey-client-tls-") as tmp:
            cert=Path(tmp)/"client.crt"; key=Path(tmp)/"client.key"
            for path,raw in ((cert,material["client_cert"]),(key,material["client_key"])):
                with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),"wb") as stream: stream.write(raw)
            self.context.load_cert_chain(cert,key)
        self.credentials={role:os.environ.get(plan["credentials"][role+"_env"],"") for role in ("probe","discovery")}
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{32,128}",v) for v in self.credentials.values()) or len(set(self.credentials.values()))!=2: raise ValueError("Supply separate scoped credentials through their environment references")
    def connection(self,name,kind,*,authenticate=True,certificate=True):
        member=next(m for m in self.contract["plan"]["members"] if m["name"]==name); pin=self.plan["members"][name]; limits=self.plan["limits"]; role="probe" if kind=="server" else "discovery"
        return Connection(pin["connect_address"],renderer.address(self.contract["plan"],member,self.contract["runtime"]),6380 if kind=="server" else 26380,
            self.context if certificate else self.bare_context,peer_sha256=pin["server_cert_sha256"],seconds=limits["request_seconds"],deadline=self.deadline,max_bytes=limits["max_response_bytes"],
            username=role if authenticate else None,password=self.credentials.get(role) if authenticate else None)


def info(value):
    if not isinstance(value,str): raise ValueError("Expected bounded INFO metadata")
    result={}
    for line in value.splitlines():
        if not line or line.startswith("#"): continue
        if ":" not in line: raise ValueError("Invalid INFO metadata")
        key,item=line.split(":",1)
        if key in result: raise ValueError("Ambiguous INFO metadata")
        result[key]=item
    return result


def pairs(value):
    if not isinstance(value,list) or len(value)%2 or any(not isinstance(v,str) for v in value) or len(set(value[::2]))!=len(value)//2: raise ValueError("Invalid Sentinel metadata")
    return dict(zip(value[::2],value[1::2]))


def snapshot(network,plan,contract):
    selected=contract["plan"]; names=list(plan["members"]); aliases={address:name for name,pin in plan["members"].items() for address in pin["addresses"]}; cache=selected["cache_name"]
    states={}; observations={}
    def resolve(address,port,expected_port):
        if str(port)!=str(expected_port) or address not in aliases: raise ValueError("Discovery pointed outside the reviewed member/port set")
        return aliases[address]
    for name in names:
        with network.connection(name,"server") as data:
            if data.command("PING")!="PONG": raise ValueError("A scoped server is not responsive")
            server=info(data.command("INFO","server")); replication=info(data.command("INFO","replication")); persistence=info(data.command("INFO","persistence")); memory=info(data.command("INFO","memory")); role=data.command("ROLE")
        if server.get("valkey_version")!=selected["version"] or server.get("run_id")!=plan["members"][name]["server_run_id"]: raise ValueError("A reviewed server invocation or release changed")
        if (not isinstance(role,list) or len(role)<3 or role[0] not in {"master","slave"} or replication.get("role")!=role[0]
                or persistence.get("loading")!="0" or persistence.get("aof_enabled")!="1" or persistence.get("aof_last_write_status")!="ok" or persistence.get("aof_last_bgrewrite_status")!="ok"
                or memory.get("maxmemory_policy")!="noeviction" or number(memory.get("maxmemory"))!=selected["settings"]["maxmemory_mib"]*1024**2): raise ValueError("A member's role, persistence or memory observation differs")
        states[name]={"role":role[0],"replication":replication,"role_reply":role}
    masters=[n for n,v in states.items() if v["role"]=="master"]
    if len(masters)!=1: raise ValueError("Require exactly one serving primary")
    primary=masters[0]; replicas=set(names)-{primary}; group_id=states[primary]["replication"].get("master_replid")
    if not identifier(group_id) or number(states[primary]["replication"].get("connected_slaves"))!=2: raise ValueError("The primary lacks the original two connected replicas")
    role_replicas=states[primary]["role_reply"][2]
    if not isinstance(role_replicas,list) or len(role_replicas)!=2 or {resolve(row[0],row[1],6380) for row in role_replicas}!=replicas: raise ValueError("The primary ROLE replica inventory differs")
    for name in replicas:
        row=states[name]["replication"]; role=states[name]["role_reply"]
        if (len(role)!=5 or role[3]!="connected" or resolve(role[1],role[2],6380)!=primary or resolve(row.get("master_host"),row.get("master_port"),6380)!=primary
                or row.get("master_link_status")!="up" or row.get("master_sync_in_progress")!="0" or row.get("master_replid")!=group_id
                or number(row.get("master_last_io_seconds_ago"))>selected["settings"]["max_lag_seconds"]): raise ValueError("A replica is detached, stale or follows another primary")
    epochs=set()
    for name in names:
        with network.connection(name,"sentinel") as sentinel:
            if sentinel.command("PING")!="PONG" or sentinel.command("SENTINEL","MYID")!=plan["members"][name]["sentinel_id"]: raise ValueError("An original Sentinel identity changed")
            server=info(sentinel.command("INFO","server")); current=pairs(sentinel.command("SENTINEL","MASTER",cache)); peers=sentinel.command("SENTINEL","SENTINELS",cache); copies=sentinel.command("SENTINEL","REPLICAS",cache)
            discovered=sentinel.command("SENTINEL","GET-MASTER-ADDR-BY-NAME",cache); quorum=sentinel.command("SENTINEL","CKQUORUM",cache)
        if (server.get("valkey_version")!=selected["version"] or not identifier(server.get("run_id")) or current.get("name")!=cache or current.get("flags")!="master"
                or resolve(current.get("ip"),current.get("port"),6380)!=primary or not isinstance(discovered,list) or len(discovered)!=2 or resolve(*discovered,6380)!=primary
                or number(current.get("num-slaves"))!=2 or number(current.get("num-other-sentinels"))!=2 or number(current.get("quorum"))!=2
                or number(current.get("down-after-milliseconds"))!=selected["settings"]["down_after_ms"] or number(current.get("failover-timeout"))!=selected["settings"]["failover_timeout_ms"]
                or number(current.get("parallel-syncs"))!=1 or not isinstance(quorum,str) or not quorum.startswith("OK 3 usable Sentinels.")): raise ValueError("Sentinel discovery, effective policy or majority differs")
        if not isinstance(peers,list) or len(peers)!=2 or not isinstance(copies,list) or len(copies)!=2: raise ValueError("The Sentinel inventory is incomplete")
        peer_names=set()
        for value in peers:
            peer=pairs(value); identity=resolve(peer.get("ip"),peer.get("port"),26380)
            if peer.get("flags")!="sentinel" or peer.get("runid")!=plan["members"][identity]["sentinel_id"]: raise ValueError("A Sentinel peer is unavailable or has another identity")
            peer_names.add(identity)
        replica_names=set()
        for value in copies:
            replica=pairs(value); identity=resolve(replica.get("ip"),replica.get("port"),6380)
            if replica.get("flags")!="slave" or replica.get("master-link-status")!="ok" or replica.get("runid")!=plan["members"][identity]["server_run_id"]: raise ValueError("A Sentinel replica is unavailable or has another invocation")
            replica_names.add(identity)
        if peer_names!=set(names)-{name} or replica_names!=replicas: raise ValueError("Sentinel peer or replica membership changed")
        epochs.add(number(current.get("config-epoch"))); observations[name]=server["run_id"]
    if len(epochs)!=1: raise ValueError("Sentinels disagree about the configuration epoch")
    return {"primary":primary,"replication_id":group_id,"epoch":next(iter(epochs)),"sentinel_invocations":observations}


def authorization(network,plan):
    foreign="__platform_valkey_acl_probe__:"+uuid4().hex
    def denied(client,code,*args):
        try: client.command(*args)
        except ReplyError as error:
            if error.code==code: return
            raise
        raise ValueError("A deliberate read-only denial was not enforced")
    for name in plan["members"]:
        with network.connection(name,"server") as data:
            denied(data,"NOPERM","GET",foreign); denied(data,"NOPERM","CONFIG","GET","protected-mode")
        with network.connection(name,"sentinel") as sentinel: denied(sentinel,"NOPERM","SENTINEL","CONFIG","GET","announce-ip")
        for kind in ("server","sentinel"):
            with network.connection(name,kind,authenticate=False) as client: denied(client,"NOAUTH","PING")
            try:
                with network.connection(name,kind,authenticate=False,certificate=False) as client: client.command("PING")
            except (ssl.SSLError,ConnectionError): continue
            raise ValueError("A TLS endpoint accepted a client without the required certificate")


def operate(bundle,plan,*,live=False,canary=False,network=None):
    contract=package(bundle,plan.get("contract_sha256")); validate(plan,contract)
    result={"schema_version":1,"local_inputs_verified":True,"live_observations_verified":False,"canary_replication_verified":False,"read_only":not canary,**{k:False for k in FALSE_FLAGS}}
    if not live:
        if canary: raise ValueError("Canary mode requires explicit live verification")
        return result
    tools=sources(Path(__file__).stem); transport=network or Network(plan,contract); before=snapshot(transport,plan,contract); authorization(transport,plan)
    if canary:
        key="platform-probe:verify-"+uuid4().hex; value=secrets.token_urlsafe(32); limits=plan["limits"]
        with transport.connection(before["primary"],"server") as primary:
            if primary.command("SET",key,value,"NX","PX",limits["canary_ttl_seconds"]*1000)!="OK" or primary.command("WAIT",2,limits["replication_wait_ms"])!=2: raise ValueError("The unique expiring canary was not acknowledged by both replicas")
        for name in plan["members"]:
            with transport.connection(name,"server") as member:
                ttl=member.command("TTL",key)
                if type(ttl) is not int or not 0<ttl<=limits["canary_ttl_seconds"] or member.command("GET",key)!=value: raise ValueError("A member lacks the exact live expiring canary")
    after=snapshot(transport,plan,contract)
    if after!=before or sources(Path(__file__).stem)!=tools: raise ValueError("Original invocations, leadership, epoch or executing source changed during observation")
    result.update(live_observations_verified=True,canary_replication_verified=canary,tls_peer_identity_observed=True,replication_links_observed=True,sentinel_membership_and_quorum_observed=True,
        probe_read_denials_observed=True,missing_client_certificate_refused=True,original_invocations_stable=True,plan_sha256=fingerprint(plan),contract_sha256=plan["contract_sha256"],
        topology_sha256=fingerprint(before),toolset_sha256=tools,recorded_at=datetime.now(timezone.utc).isoformat())
    result["receipt_sha256"]=fingerprint(result); return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--bundle-dir",type=Path,required=True); parser.add_argument("--runtime-plan",type=Path,required=True); parser.add_argument("--proof",type=Path)
    mode=parser.add_mutually_exclusive_group(); mode.add_argument("--verify",action="store_true"); mode.add_argument("--canary",action="store_true"); args=parser.parse_args(argv)
    try:
        plan=loads_strict_json(read_bounded_bytes(args.runtime_plan,max_bytes=128*1024)); result=operate(args.bundle_dir,plan,live=args.verify or args.canary,canary=args.canary)
        if args.proof is not None:
            if not (args.verify or args.canary): raise ValueError("Only live observations may write a receipt")
            atomic_write_text(args.proof,canonical(result))
        print(canonical(result)); return 0
    except Exception:
        print("Valkey runtime verification failed; endpoints, credentials and response details suppressed.",file=sys.stderr); return 1


if __name__=="__main__": raise SystemExit(main())
