#!/usr/bin/env python3
"""Activate one prepared native/Docker Valkey and Sentinel pair with original custody."""

import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import sys
import time
from uuid import uuid4

import activate_keycloak_host as mechanics
import activate_openbao_host as host_policy
from atomic_file import atomic_write_text,_fsync_directory
from bootstrap_keycloak_realm import sources
from bounded_file import read_bootstrap_bytes,read_bounded_bytes
from deploy_keycloak_ha import canonical,fingerprint,lock,sha,uid
import render_valkey_ha as renderer
from strict_json import loads_strict_json
from valkey_protocol import Connection
import verify_valkey_runtime as observer


ROOT=Path(__file__).resolve().parents[1]
KINDS=("server","sentinel")
FILES={"tls/tls.crt","tls/tls.key","tls/ca.crt","auth/server.acl","auth/sentinel.acl","auth/server-auth.conf","auth/sentinel-auth.conf"}
UNVERIFIED=("replication_verified","sentinel_quorum_verified","application_compatibility_verified","lossless_failover_verified","physical_node_loss_verified","storage_durability_verified","ha_verified","full_stack_verified")
LABEL="platform.gitops/valkey-"


def tools(): return sources(Path(__file__).stem)
def member(contract,plan): return next(m for m in contract["plan"]["members"] if m["name"]==plan["member"])
def name(plan,kind): return "platform-valkey-"+plan["member"]+"-"+kind
def labels(state): return {LABEL+k:state[v] for k,v in (("operation","operation_id"),("plan","plan_sha256"),("tools","toolset_sha256"))}
def private(contract,plan): return Path(contract["plan"]["private_dir"])/plan["member"]
def directory(contract,plan,kind): return Path(member(contract,plan)[kind+"_dir"])


def package(bundle,digest):
    contract=observer.package(bundle,digest)
    if contract["runtime"] not in {"raw","docker"}: raise ValueError("Select the native or Docker host package")
    return contract


def validate(plan,contract):
    renderer.exact(plan,"schema_version contract_sha256 member machine_id_sha256 boot_id service_uid service_gid files_sha256 credentials storage docker native")
    if not isinstance(plan["files_sha256"],dict) or set(plan["files_sha256"])!=FILES or not all(sha(v) for v in plan["files_sha256"].values()): raise ValueError("Pin the seven original private inputs")
    renderer.exact(plan["credentials"],"probe_env discovery_env")
    if any(not observer.reference(v) for v in plan["credentials"].values()) or len(set(plan["credentials"].values()))!=2: raise ValueError("Reference separate scoped health credentials")
    renderer.exact(plan["storage"],"server sentinel")
    # Reuse only host identity, numeric account and local transport pin validation.
    projected={k:v for k,v in plan.items() if k!="credentials"}
    projected["files_sha256"]={k:v for k,v in plan["files_sha256"].items() if k.startswith("tls/")}
    projected["storage"]={"data":plan["storage"]["server"],"audit":plan["storage"]["sentinel"]}
    host_policy.validate(projected,contract)
    return plan


def unit_text(contract,plan,state,kind):
    text=renderer.render(contract["plan"],"raw")[plan["member"]+"-"+kind+".service"]
    text=text.replace("User=valkey\n","User="+plan["native"]["user"]+"\n").replace("Group=valkey\n","Group="+plan["native"]["group"]+"\n")
    limit=plan["native"]["max_runtime_seconds"]
    return "# valkey-operation="+state["operation_id"]+"\n# valkey-plan="+fingerprint(plan)+"\n"+text+("\n[Service]\nRestart=no\nRuntimeMaxSec="+str(limit)+"\n" if limit is not None else "")


def mounts(contract,plan,kind):
    prepared=private(contract,plan)
    return [(str(directory(contract,plan,kind)),"/state/"+kind,False),*[(str(prepared/k),"/run/platform-valkey/"+k,True) for k in ("tls","auth")]]


def memory(contract,kind): return (contract["plan"]["settings"]["maxmemory_mib"]*2+256 if kind=="server" else 128)*1024**2
def command(kind): return [*renderer.ENTRYPOINT[1:],"/state/"+kind+"/"+kind+".conf",*(["--sentinel"] if kind=="sentinel" else [])]


def create_arguments(contract,plan,state,kind):
    size=str(memory(contract,kind)); args=["container","create","--pull=never","--name",name(plan,kind),"--user=1000:1000","--network=host","--read-only","--cap-drop=ALL",
        "--security-opt=no-new-privileges:true","--memory="+size,"--memory-swap="+size,"--cpus=2","--pids-limit=256","--ipc=none","--cgroupns=private","--userns=host","--runtime=runc",
        "--restart=unless-stopped","--stop-timeout=60","--workdir=/","--hostname="+plan["member"],"--no-healthcheck","--log-driver=json-file","--log-opt=max-size=10m","--log-opt=max-file=3","--entrypoint=/bin/sh"]
    for k,v in sorted(labels(state).items()): args += ["--label",k+"="+v]
    for source,target,readonly in mounts(contract,plan,kind): args += ["--mount","type=bind,source="+source+",destination="+target+(",readonly" if readonly else "")+",bind-propagation=rprivate"]
    return [*args,"--tmpfs","/tmp:rw,nosuid,nodev,noexec,mode=1777,size=64m",contract["plan"]["image"],*command(kind)]


def container_policy(row,contract,plan,state,facts,kind):
    resource=state["resources"][kind]; config=row.get("Config",{}); host=row.get("HostConfig",{})
    host={**host,"OomKillDisable":False} if host.get("OomKillDisable") is None else host
    if (not mechanics.container_id(row.get("Id")) or resource["handle"] is not None and row["Id"]!=resource["handle"] or row.get("Name")!="/"+name(plan,kind)
            or row.get("Image")!=plan["docker"]["image_id"] or config.get("Image")!=contract["plan"]["image"] or config.get("User")!="1000:1000"
            or config.get("Entrypoint")!=[renderer.ENTRYPOINT[0]] or config.get("Cmd")!=command(kind)
            or mechanics.environment_map(config.get("Env") or [])!=mechanics.environment_map(facts["image"].get("Config",{}).get("Env") or [])
            or config.get("Labels")!={**(facts["image"].get("Config",{}).get("Labels") or {}),**labels(state)} or config.get("WorkingDir")!="/"
            or config.get("Hostname")!=plan["member"] or config.get("StopTimeout")!=60 or config.get("Tty") is not False or config.get("OpenStdin") is not False
            or config.get("Healthcheck",{}).get("Test")!=["NONE"]): raise ValueError("The original Docker identity or command changed")
    expected={"ReadonlyRootfs":True,"Privileged":False,"OomKillDisable":False,"NetworkMode":"host","IpcMode":"none","CgroupnsMode":"private","UsernsMode":"host","Runtime":"runc",
        "PidsLimit":256,"Memory":memory(contract,kind),"MemorySwap":memory(contract,kind),"NanoCpus":2000000000,"AutoRemove":False,
        "RestartPolicy":{"Name":"unless-stopped","MaximumRetryCount":0},"LogConfig":{"Type":"json-file","Config":{"max-size":"10m","max-file":"3"}}}
    if (any(host.get(k)!=v for k,v in expected.items()) or host.get("CapDrop")!=["ALL"] or host.get("SecurityOpt") not in (["no-new-privileges:true"],["no-new-privileges"])
            or any(host.get(k) for k in ("CapAdd","Binds","VolumesFrom","Devices","DeviceRequests","DeviceCgroupRules","GroupAdd","Links","PortBindings","PublishAllPorts","PidMode","UTSMode","ExtraHosts","Sysctls","Init"))
            or host.get("Tmpfs")!={"/tmp":"rw,nosuid,nodev,noexec,mode=1777,size=64m"}): raise ValueError("Original Docker isolation changed")
    expected_mounts={target:(source,not readonly) for source,target,readonly in mounts(contract,plan,kind)}; actual=row.get("Mounts",[])
    if (len(actual)!=3 or {m.get("Destination") for m in actual}!=set(expected_mounts) or len(host.get("Mounts",[]))!=3
            or any(m.get("Type")!="bind" or (m.get("Source"),m.get("RW"))!=expected_mounts[m["Destination"]] or m.get("Propagation")!="rprivate" for m in actual)):
        raise ValueError("Original private mounts changed")
    return fingerprint({"Config":config,"HostConfig":host,"Mounts":sorted(actual,key=lambda m:m["Destination"]),"Image":row["Image"]})


class Host(mechanics.Host):
    def unit_path(self,plan,kind): return Path("/etc/systemd/system")/(name(plan,kind)+".service")
    def properties(self,plan,kind):
        raw=self.run(["/usr/bin/systemctl","show",name(plan,kind)+".service","--property=LoadState,FragmentPath,DropInPaths,ActiveState,SubState,MainPID,InvocationID,NRestarts,NeedDaemonReload,ControlGroup"])
        return dict(line.split("=",1) for line in raw.splitlines() if "=" in line)
    def prerequisites(self,contract,plan,*,starting=False):
        if sys.platform!="linux" or os.geteuid()!=0: raise ValueError("Select the root Linux host explicitly")
        for path in (Path(sys.executable).resolve(strict=True),*Path(__file__).parent.glob("*.py")): mechanics.trusted(path)
        machine=read_bounded_bytes(Path("/etc/machine-id"),max_bytes=128).decode().strip(); boot=read_bounded_bytes(Path("/proc/sys/kernel/random/boot_id"),max_bytes=128).decode().strip()
        if hashlib.sha256(machine.encode()).hexdigest()!=plan["machine_id_sha256"] or boot!=plan["boot_id"]: raise ValueError("The reviewed machine or boot changed")
        selected=contract["plan"]; chosen=member(contract,plan); prepared=private(contract,plan)
        ip=Path("/usr/sbin/ip").resolve(strict=True); mechanics.trusted(ip); rows=loads_strict_json(self.run([str(ip),"-j","-4","address","show"]))
        if sum(a.get("local")==chosen["address"] for row in rows for a in row.get("addr_info",[]))!=1: raise ValueError("The approved member address is not uniquely local")
        if starting:
            for port in (6380,26380):
                with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as reservation: reservation.bind((chosen["address"],port))
        mechanics.trusted(prepared,directory=True)
        for kind,names in (("tls",{"tls.crt","tls.key","ca.crt"}),("auth",{"server.acl","sentinel.acl","server-auth.conf","sentinel-auth.conf"})):
            base=prepared/kind; mechanics.trusted(base,directory=True)
            if {p.name for p in base.iterdir()}!=names: raise ValueError("The prepared private mount inventory changed")
        for filename,digest in plan["files_sha256"].items():
            path=prepared/filename; info=mechanics.trusted(path)
            if filename.startswith("auth/") or filename=="tls/tls.key":
                if info.st_gid!=plan["service_gid"] or stat.S_IMODE(info.st_mode) not in {0o440,0o640}: raise ValueError("Keep private inputs root-owned and readable only by the dedicated service group")
            if mechanics.file_hash(path,maximum=128*1024)!=digest: raise ValueError("A prepared private input changed")
            mechanics.readable(path,plan,boundary=prepared/filename.split("/")[0] if contract["runtime"]=="docker" else None)
        ctx=ssl.create_default_context(cadata=read_bounded_bytes(prepared/"tls/ca.crt",max_bytes=128*1024).decode("ascii")); ctx.load_cert_chain(prepared/"tls/tls.crt",prepared/"tls/tls.key")
        credentials={role:os.environ.get(plan["credentials"][role+"_env"],"") for role in ("probe","discovery")}
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{32,128}",v) for v in credentials.values()) or len(set(credentials.values()))!=2: raise ValueError("Supply both scoped private health credentials before activation")
        for kind in KINDS:
            path=directory(contract,plan,kind); info=mechanics.trusted(path,owner=plan["service_uid"],private=True,directory=True)
            if info.st_gid!=plan["service_gid"] or stat.S_IMODE(info.st_mode)!=0o700 or {"device":info.st_dev,"inode":info.st_ino}!=plan["storage"][kind] or starting and any(path.iterdir()): raise ValueError("Original private storage or fresh scope changed")
        result={"machine_id_sha256":plan["machine_id_sha256"],"boot_id":boot,"storage":plan["storage"],"credentials_sha256":fingerprint(credentials)}
        if contract["runtime"]=="docker":
            pins=plan["docker"]
            if (self.binary,self.socket)!=mechanics.docker_paths(pins) or mechanics.file_hash(Path(self.binary))!=pins["binary_sha256"]: raise ValueError("The explicit local Docker transport changed")
            sock,info=mechanics.protected_socket(self.socket); facts=self.docker_facts(); version=loads_strict_json(self.docker("version","--format",'{{json .Server.Version}}'))
            if (facts.get("id")!=pins["daemon_id"] or version!=pins["server_version"] or facts.get("os")!="linux" or facts.get("runtime")!="runc" or facts.get("cgroup")!="2"
                    or not isinstance(facts.get("security"),list) or any("rootless" in s or "userns" in s for s in facts["security"])
                    or not any("name=seccomp" in s and "profile=builtin" in s for s in facts["security"]) or any(facts.get(k) is not True for k in ("memory","swap","cpu_period","cpu_quota","pids"))): raise ValueError("The reviewed engine or resource enforcement changed")
            images=loads_strict_json(self.docker("image","inspect",selected["image"]))
            if (len(images)!=1 or images[0].get("Id")!=pins["image_id"] or selected["image"] not in images[0].get("RepoDigests",[]) or images[0].get("Os")!="linux"
                    or images[0].get("Architecture")!={"x86_64":"amd64","aarch64":"arm64"}.get(os.uname().machine) or images[0].get("Config",{}).get("Volumes")): raise ValueError("Preload the exact reviewed image without anonymous volumes")
            result.update(socket_path=str(sock),socket_device=info.st_dev,socket_inode=info.st_ino,image={k:images[0][k] for k in ("Id","Config")})
        else:
            import pwd,grp
            pins=plan["native"]; account=pwd.getpwnam(pins["user"])
            if account.pw_uid!=plan["service_uid"] or account.pw_gid!=plan["service_gid"] or grp.getgrnam(pins["group"]).gr_gid!=plan["service_gid"]: raise ValueError("The native account changed")
            binary=Path(selected["binary"]); mechanics.readable(binary,plan,executable=True)
            if mechanics.file_hash(binary)!=selected["binary_sha256"] or mechanics.file_hash(Path("/usr/bin/systemctl"))!=pins["systemctl_sha256"]: raise ValueError("A reviewed executable changed")
            if not self.run([str(binary),"--version"]).startswith("Valkey server v="+selected["version"]+" "): raise ValueError("The native release differs")
            checksum=prepared/"binary.sha256"; mechanics.trusted(checksum); mechanics.readable(checksum,plan)
            if read_bounded_bytes(checksum,max_bytes=4096).decode()!=selected["binary_sha256"]+"  "+selected["binary"]+"\n": raise ValueError("The native executable commitment changed")
        return result
    def empty(self,contract,plan,kind):
        if any(directory(contract,plan,kind).iterdir()): raise ValueError("Never seed nonempty original storage")
    def seed(self,contract,plan,state,kind):
        self.empty(contract,plan,kind); root=directory(contract,plan,kind)
        content=renderer.configuration(contract["plan"],member(contract,plan),contract["runtime"],kind)
        marker=canonical({"operation_id":state["operation_id"],"plan_sha256":state["plan_sha256"],"seed_sha256":hashlib.sha256(content.encode()).hexdigest()})
        for path,value,owner,mode in ((root/(kind+".conf"),content,plan["service_uid"],0o600),(root/".platform-original.json",marker,0,0o644)):
            descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,mode); os.fchmod(descriptor,mode); os.fchown(descriptor,owner,plan["service_gid"])
            with os.fdopen(descriptor,"w",encoding="utf-8",newline="\n") as stream: stream.write(value); stream.flush(); os.fsync(stream.fileno())
        _fsync_directory(root)
    def inspect_seed(self,contract,plan,state,kind):
        root=directory(contract,plan,kind); marker=root/".platform-original.json"; meta=marker.lstat(); path=root/(kind+".conf"); config=path.lstat()
        if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink!=1 or meta.st_uid!=0 or stat.S_IMODE(meta.st_mode)!=0o644 or not stat.S_ISREG(config.st_mode)
                or config.st_nlink!=1 or config.st_uid!=plan["service_uid"] or config.st_gid!=plan["service_gid"] or stat.S_IMODE(config.st_mode)!=0o600): raise ValueError("Original mutable configuration or marker access changed")
        expected=renderer.configuration(contract["plan"],member(contract,plan),contract["runtime"],kind).encode()
        wanted={"operation_id":state["operation_id"],"plan_sha256":state["plan_sha256"],"seed_sha256":hashlib.sha256(expected).hexdigest()}
        if loads_strict_json(read_bootstrap_bytes(marker,max_bytes=4096,follow_symlinks=False))!=wanted: raise ValueError("The state belongs to another operation")
        if not state["resources"][kind]["start_intent"] and read_bootstrap_bytes(path,max_bytes=128*1024,follow_symlinks=False)!=expected: raise ValueError("The original seed changed before start")
        return fingerprint({"device":meta.st_dev,"inode":meta.st_ino})
    def existing(self,contract,plan,kind):
        if contract["runtime"]=="docker":
            ids=self.docker("container","ls","--all","--no-trunc","--filter","name=^/"+name(plan,kind)+"$","--format",'{{.ID}}').split()
            if len(ids)>1 or any(not mechanics.container_id(v) for v in ids): raise ValueError("Ambiguous original container")
            return ids[0] if ids else None
        path=self.unit_path(plan,kind)
        return "existing-unit" if path.exists() or path.is_symlink() or self.properties(plan,kind).get("LoadState")!="not-found" else None
    def create(self,contract,plan,state,kind):
        if contract["runtime"]=="docker":
            if not mechanics.container_id(self.docker(*create_arguments(contract,plan,state,kind)).strip()): raise ValueError("The original create was not acknowledged")
        else:
            path=self.unit_path(plan,kind); mechanics.trusted(path.parent,directory=True); descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o644); os.fchmod(descriptor,0o644)
            with os.fdopen(descriptor,"w",encoding="utf-8",newline="\n") as stream: stream.write(unit_text(contract,plan,state,kind)); stream.flush(); os.fsync(stream.fileno())
            _fsync_directory(path.parent)
    def inspect(self,contract,plan,state,facts,kind):
        resource=state["resources"][kind]
        if contract["runtime"]=="docker":
            rows=loads_strict_json(self.docker("container","inspect",name(plan,kind)))
            if len(rows)!=1: raise ValueError("The original container is absent")
            row=rows[0]; policy=container_policy(row,contract,plan,state,facts,kind); live=row.get("State",{})
            if live.get("Dead") or live.get("OOMKilled") or live.get("Error") or live.get("Status") not in {"created","running"} or live.get("Running") is not (live.get("Status")=="running"): raise ValueError("The original container failed")
            active=live["Running"]; invocation=live.get("StartedAt") if active else None; pid=live.get("Pid") if active else None; cgroup=mechanics.process_cgroup(pid) if active else None
            if active and (not isinstance(invocation,str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z",invocation) or datetime.fromisoformat(invocation.replace("Z","+00:00")).year<2020 or row["Id"] not in cgroup): raise ValueError("Original container invocation or cgroup changed")
            return {"handle":row["Id"],"policy_sha256":policy,"active":active,"invocation":invocation,"loaded":True,"pid":pid,"cgroup":cgroup}
        path=self.unit_path(plan,kind); meta=mechanics.trusted(path); expected=unit_text(contract,plan,state,kind).encode(); props=self.properties(plan,kind)
        if read_bounded_bytes(path,max_bytes=64*1024)!=expected or stat.S_IMODE(meta.st_mode)!=0o644: raise ValueError("The original unit changed")
        if props.get("DropInPaths") or props.get("FragmentPath") not in {"",str(path)} or props.get("ActiveState") in {"failed","deactivating"}: raise ValueError("The original unit ownership or state changed")
        active=props.get("ActiveState")=="active" and props.get("SubState")=="running" and str(props.get("MainPID","")).isdigit() and int(props["MainPID"])>1
        invocation=props.get("InvocationID") or None; cgroup=props.get("ControlGroup") or None
        if invocation is not None and not re.fullmatch(r"[0-9a-f]{32}",invocation): raise ValueError("Invalid original invocation")
        if active and (not cgroup or not cgroup.endswith("/"+name(plan,kind)+".service") or mechanics.process_cgroup(int(props["MainPID"]))!=cgroup): raise ValueError("The service escaped its original cgroup")
        return {"handle":fingerprint({"device":meta.st_dev,"inode":meta.st_ino}),"policy_sha256":hashlib.sha256(expected).hexdigest(),"active":active,"invocation":invocation,
            "loaded":props.get("LoadState")=="loaded" and props.get("FragmentPath")==str(path) and props.get("NeedDaemonReload")=="no","pid":int(props["MainPID"]) if active else None,"cgroup":cgroup}
    def start(self,contract,plan,state,kind):
        if contract["runtime"]=="docker": self.docker("container","start",state["resources"][kind]["handle"])
        else: self.run(["/usr/bin/systemctl","start","--no-block",name(plan,kind)+".service"])
    def healthy(self,contract,plan,original,kind):
        chosen=member(contract,plan); address=chosen["address"]; port=6380 if kind=="server" else 26380
        if not mechanics.owns_listener(original,address,port): return None
        prepared=private(contract,plan); ctx=ssl.create_default_context(cadata=read_bounded_bytes(prepared/"tls/ca.crt",max_bytes=128*1024).decode("ascii")); ctx.load_cert_chain(prepared/"tls/tls.crt",prepared/"tls/tls.key")
        cert=ssl.PEM_cert_to_DER_cert(read_bounded_bytes(prepared/"tls/tls.crt",max_bytes=128*1024).decode("ascii")); role="probe" if kind=="server" else "discovery"
        try:
            with Connection(address,address,port,ctx,peer_sha256=hashlib.sha256(cert).hexdigest(),seconds=3,deadline=time.monotonic()+10,max_bytes=256*1024,username=role,password=os.environ.get(plan["credentials"][role+"_env"])) as client:
                if client.command("PING")!="PONG": return None
                server=observer.info(client.command("INFO","server"))
                if server.get("valkey_version")!=contract["plan"]["version"] or not observer.identifier(server.get("run_id")): return None
                if kind=="server":
                    current=client.command("ROLE")
                    if not isinstance(current,list) or not current or current[0] not in {"master","slave"}: return None
                    identity={"run_id":server["run_id"]}
                else:
                    sentinel=client.command("SENTINEL","MYID")
                    if not observer.identifier(sentinel): return None
                    identity={"run_id":server["run_id"],"sentinel_id":sentinel}
                return fingerprint(identity)
        except (OSError,ValueError): return None


def operate(bundle,plan,*,mode="local",output=None,host=None,wait_seconds=120):
    contract=package(bundle,plan.get("contract_sha256")); validate(plan,contract)
    result={"local_inputs_verified":True,"host_checked":False,"original_pair_active":False,"tls_authenticated_processes_verified":False,"read_only":True,**{k:False for k in UNVERIFIED}}
    if mode=="local": return result
    if mode not in {"check-host","activate","resume"} or type(wait_seconds) is not int or not 5<=wait_seconds<=600: raise ValueError("Select a bounded explicit host operation")
    backend=host or Host(plan["docker"]); facts=backend.prerequisites(contract,plan,starting=mode!="resume"); source=tools()
    if mode=="check-host":
        if any(backend.existing(contract,plan,k) is not None for k in KINDS): raise ValueError("Existing services require original-operation recovery")
        return {**result,"host_checked":True}
    if output is None or output.is_symlink(): raise ValueError("Select the original private journal directory")
    output=output.resolve(); relative=output.relative_to((ROOT/"private").resolve())
    if not relative.parts: raise ValueError("Select a member-specific journal")
    if mode=="activate": output.mkdir(parents=True,mode=0o700,exist_ok=False)
    if not output.is_dir(): raise ValueError("Resume requires the original operation")
    receipt=output/"valkey-host-proof.json"
    with lock(output):
        if mode=="resume":
            if receipt.is_symlink(): raise ValueError("The original journal cannot redirect")
            state=loads_strict_json(read_bounded_bytes(receipt,max_bytes=256*1024))
            if (type(state.get("schema_version")) is not int or state["schema_version"]!=1 or state.get("plan_sha256")!=fingerprint(plan) or state.get("toolset_sha256")!=source or state.get("facts_sha256")!=fingerprint(facts)
                    or not uid(state.get("operation_id")) or any(state.get(k) is not False for k in UNVERIFIED) or not isinstance(state.get("resources"),dict) or set(state["resources"])!=set(KINDS)): raise ValueError("Resume only the original host and operation")
            for resource in state["resources"].values():
                renderer.exact(resource,"seed_intent create_intent reload_intent start_intent seed_handle handle policy_sha256 invocation protocol_sha256")
                if (any(type(resource[k]) is not bool for k in ("seed_intent","create_intent","reload_intent","start_intent")) or resource["create_intent"] and not resource["seed_intent"]
                        or resource["reload_intent"] and (not resource["create_intent"] or contract["runtime"]=="docker") or resource["start_intent"] and (not resource["create_intent"] or contract["runtime"]=="raw" and not resource["reload_intent"])
                        or any(resource[k] is not None and not sha(resource[k]) for k in ("seed_handle","handle","policy_sha256","protocol_sha256"))): raise ValueError("Original operation custody is invalid")
        else:
            if any(backend.existing(contract,plan,k) is not None for k in KINDS): raise ValueError("Never adopt or replace an existing service")
            state={"schema_version":1,"operation_id":str(uuid4()),"plan_sha256":fingerprint(plan),"toolset_sha256":source,"facts_sha256":fingerprint(facts),**result,
                "resources":{k:{**{v:False for v in ("seed_intent","create_intent","reload_intent","start_intent")},**{v:None for v in ("seed_handle","handle","policy_sha256","invocation","protocol_sha256")}} for k in KINDS}}
        wrote=False; deadline=time.monotonic()+wait_seconds
        def record(phase,**values):
            state.update(phase=phase,recorded_at=datetime.now(timezone.utc).isoformat(),**values); atomic_write_text(receipt,canonical(state))
        def unchanged():
            if tools()!=source or fingerprint(backend.prerequisites(contract,plan,starting=False))!=state["facts_sha256"]: raise ValueError("Original prepared inputs changed")
        def seed_handle(kind):
            resource=state["resources"][kind]; found=backend.inspect_seed(contract,plan,state,kind)
            if resource["seed_handle"] is not None and resource["seed_handle"]!=found: raise ValueError("Original state marker was replaced")
            resource["seed_handle"]=found
        def inspect(kind):
            seed_handle(kind); resource=state["resources"][kind]; observed=backend.inspect(contract,plan,state,facts,kind)
            if any(resource[k] is not None and resource[k]!=observed[k] for k in ("handle","policy_sha256","invocation")): raise ValueError("The original service was replaced or restarted")
            resource.update(handle=observed["handle"],policy_sha256=observed["policy_sha256"])
            if observed["active"]:
                if observed["invocation"] is None: raise ValueError("The active original service lacks its invocation")
                resource["invocation"]=observed["invocation"]
            return observed
        try:
            record("prepared",**result)
            for kind in KINDS:
                resource=state["resources"][kind]
                if not resource["seed_intent"]:
                    unchanged(); backend.empty(contract,plan,kind); resource["seed_intent"]=True; wrote=True; record("before-seed-"+kind,read_only=False); backend.seed(contract,plan,state,kind)
                seed_handle(kind); record("original-seed-"+kind)
                if not resource["create_intent"]:
                    unchanged()
                    if backend.existing(contract,plan,kind) is not None: raise ValueError("An unrelated service appeared")
                    resource["create_intent"]=True; wrote=True; record("before-create-"+kind,read_only=False); backend.create(contract,plan,state,kind)
                observed=inspect(kind); record("original-created-"+kind)
                if observed["active"] and not resource["start_intent"]: raise ValueError("A service started outside its original operation")
                if contract["runtime"]=="raw" and not resource["reload_intent"]:
                    unchanged(); resource["reload_intent"]=True; wrote=True; record("before-reload-"+kind,read_only=False); backend.reload()
                if not inspect(kind)["loaded"]: raise ValueError("Observe the uncertain original reload without repeating it")
                if not resource["start_intent"]:
                    unchanged(); seed_handle(kind); resource["start_intent"]=True; wrote=True; record("before-start-"+kind,read_only=False); backend.start(contract,plan,state,kind)
                while True:
                    observed=inspect(kind); protocol=backend.healthy(contract,plan,observed,kind) if observed["active"] else None
                    if protocol is not None:
                        if not sha(protocol) or resource["protocol_sha256"] is not None and resource["protocol_sha256"]!=protocol: raise ValueError("The original protocol invocation changed")
                        resource["protocol_sha256"]=protocol; record("observed-original-"+kind); break
                    if time.monotonic()>=deadline: raise TimeoutError("Continue observing the same original service without repeating start")
                    time.sleep(1)
            unchanged()
            for kind in KINDS:
                observed=inspect(kind)
                if not observed["active"] or backend.healthy(contract,plan,observed,kind)!=state["resources"][kind]["protocol_sha256"]: raise ValueError("An original authenticated service became unavailable")
            result.update(host_checked=True,original_pair_active=True,tls_authenticated_processes_verified=True,read_only=not wrote)
            record("awaiting-group-runtime-verification",**result); return result
        except BaseException:
            record("failed",failed_at=state["phase"],error_code="valkey-host-activation-unverified"); raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--bundle-dir",type=Path,required=True); parser.add_argument("--host-plan",type=Path,required=True); parser.add_argument("--output-dir",type=Path); parser.add_argument("--wait-seconds",type=int,default=120)
    group=parser.add_mutually_exclusive_group()
    for mode in ("check-host","activate","resume"): group.add_argument("--"+mode,action="store_true")
    args=parser.parse_args(argv)
    try:
        plan=loads_strict_json(read_bounded_bytes(args.host_plan,max_bytes=128*1024)); mode=next((m.replace("_","-") for m in ("check_host","activate","resume") if getattr(args,m)),"local")
        print(canonical(operate(args.bundle_dir,plan,mode=mode,output=args.output_dir,wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("Valkey host activation stopped; retain the original private journal and handles. Details suppressed.",file=sys.stderr); return 1


if __name__=="__main__": raise SystemExit(main())
