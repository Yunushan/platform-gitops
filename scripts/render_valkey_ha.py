#!/usr/bin/env python3
"""Render TLS Valkey replication/Sentinel packages; no application compatibility claim."""

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shlex
import sys

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE, RUNTIMES, dns_name, write_bundle
from render_openbao_ha import exact, label, PATH
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
LABELS = {"app.kubernetes.io/name": "platform-valkey-sentinel"}
ENTRYPOINT = ["/bin/sh", "-c", 'umask 077; exec /usr/local/bin/valkey-server "$@"', "platform-valkey"]


def validate(plan):
    exact(plan, "schema_version version image tools_image namespace cluster_domain private_dir binary binary_sha256 primary cache_name members settings network")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or plan["version"] != "8.1.11": raise ValueError("Use the reviewed Valkey 8.1.11 family")
    if any(not isinstance(plan[k],str) or not IMAGE.fullmatch(plan[k]) for k in ("image","tools_image")): raise ValueError("Pin both workload and seed-tool images by digest")
    if not label(plan["namespace"]) or not dns_name(plan["cluster_domain"]) or not label(plan["cache_name"]): raise ValueError("Select explicit cluster and cache identities")
    if not isinstance(plan["binary_sha256"],str) or not re.fullmatch(r"[0-9a-f]{64}",plan["binary_sha256"]): raise ValueError("Pin the TLS-enabled native executable")
    paths=[plan[k] for k in ("private_dir","binary")]; members=plan["members"]
    if not isinstance(members,list) or len(members)!=3: raise ValueError("Require three data members and three independent Sentinels")
    for m in members:
        exact(m,"name node address failure_domain server_dir sentinel_dir server_claim sentinel_claim tls_secret auth_secret")
        if any(not label(m[k]) for k in ("name","failure_domain","server_claim","sentinel_claim","tls_secret","auth_secret")) or not dns_name(m["node"]): raise ValueError("Use explicit member and private-input references")
        address=ipaddress.IPv4Address(m["address"])
        if str(address)!=m["address"] or address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local or address.is_reserved: raise ValueError("Declare routable member addresses")
        paths.extend((m["server_dir"],m["sentinel_dir"]))
    for key in ("name","node","address","failure_domain","server_claim","sentinel_claim","tls_secret","auth_secret"):
        if len({m[key] for m in members})!=3: raise ValueError("Separate member, host, storage, secret and failure-domain identities")
    if len({m[k] for m in members for k in ("server_claim","sentinel_claim")})!=6 or plan["primary"] not in {m["name"] for m in members}: raise ValueError("Pin the original primary and six separate state claims")
    if (any(not isinstance(p,str) or not PATH.fullmatch(p) or any(c in {".",".."} for c in p.split("/")) for p in paths)
            or len(set(paths))!=len(paths) or any(b.startswith(a+"/") for a in paths for b in paths if a!=b)): raise ValueError("Keep credentials, executable and mutable state paths canonical and disjoint")
    settings=plan["settings"]; exact(settings,"maxmemory_mib min_replicas max_lag_seconds down_after_ms failover_timeout_ms")
    bounds={"maxmemory_mib":(64,2048),"min_replicas":(1,1),"max_lag_seconds":(1,10),"down_after_ms":(2000,30000),"failover_timeout_ms":(15000,180000)}
    if any(type(settings[k]) is not int or not low<=settings[k]<=high for k,(low,high) in bounds.items()) or settings["failover_timeout_ms"]<settings["down_after_ms"]*3: raise ValueError("Bound memory, write admission and Sentinel detection/election intervals")
    exact(plan["network"],"client_namespaces")
    clients=plan["network"]["client_namespaces"]
    if not isinstance(clients,list) or not 1<=len(clients)<=8 or not all(label(v) for v in clients) or len(set(clients))!=len(clients): raise ValueError("Inventory client namespaces explicitly")
    return plan


def address(plan,member,runtime):
    return member["address"] if runtime in {"raw","docker"} else member["name"]+"."+plan["namespace"]+".svc."+plan["cluster_domain"]


def locations(plan,member,runtime):
    return (plan["private_dir"]+"/"+member["name"],member["server_dir"],member["sentinel_dir"]) if runtime=="raw" else ("/run/platform-valkey","/state/server","/state/sentinel")


def configuration(plan,member,runtime,kind):
    if kind not in {"server","sentinel"}: raise ValueError("Select server or Sentinel")
    private,server,sentinel=locations(plan,member,runtime); host=address(plan,member,runtime); bind=member["address"] if runtime in {"raw","docker"} else "0.0.0.0"
    port=6380 if kind=="server" else 26380; state=server if kind=="server" else sentinel
    common=["bind "+bind,"protected-mode yes","port 0","tls-port "+str(port),"tls-cert-file "+private+"/tls/tls.crt","tls-key-file "+private+"/tls/tls.key",
        "tls-ca-cert-file "+private+"/tls/ca.crt","tls-auth-clients yes","tls-replication yes",'tls-protocols "TLSv1.2 TLSv1.3"',"daemonize no","logfile \"\"","loglevel notice","dir "+state,
        ]
    if kind=="server":
        common += ["maxmemory "+str(plan["settings"]["maxmemory_mib"])+"mb","maxmemory-policy noeviction","appendonly yes","appendfsync everysec",'save ""',
            "replica-read-only yes","replica-serve-stale-data no","repl-diskless-sync yes","repl-diskless-sync-delay 0","repl-ping-replica-period 1","min-replicas-to-write 1",
            "min-replicas-max-lag "+str(plan["settings"]["max_lag_seconds"]),"replica-announce-ip "+host,"replica-announce-port 6380"]
        if member["name"]!=plan["primary"]:
            primary=next(m for m in plan["members"] if m["name"]==plan["primary"]); common += ["replicaof "+address(plan,primary,runtime)+" 6380"]
    else:
        primary=next(m for m in plan["members"] if m["name"]==plan["primary"]); cache=plan["cache_name"]
        common += ["sentinel resolve-hostnames yes","sentinel announce-hostnames yes","sentinel announce-ip "+host,"sentinel announce-port 26380",
            "sentinel monitor "+cache+" "+address(plan,primary,runtime)+" 6380 2","sentinel down-after-milliseconds "+cache+" "+str(plan["settings"]["down_after_ms"]),
            "sentinel failover-timeout "+cache+" "+str(plan["settings"]["failover_timeout_ms"]),"sentinel parallel-syncs "+cache+" 1"]
    # Sentinel auth-user/auth-pass name an already declared monitor.
    return "\n".join([*common,"include "+private+"/auth/"+kind+"-auth.conf"])+"\n"


def seed_script(server_hash,sentinel_hash,*,volume_root="/volumes",seed_root="/seed"):
    # Role and Sentinel state are rewritten by the daemon after failover. Only
    # seed fresh runtime directories; a partial seed is never silently adopted.
    return "\n".join(["set -eu","umask 077",'operation=${VALKEY_INSTALL_OPERATION_ID:?}',
        'test "${#operation}" = 32', 'case "$operation" in *[!0-9a-f]*) exit 43;; esac',*[
        f'''test "$(sha256sum {seed_root}/{kind}.conf | cut -d ' ' -f 1)" = '{digest}'
base={volume_root}/{kind}
target="$base/runtime"
if [ ! -e "$target" ]; then
  mkdir -m 700 "$target"
  (set -C; cat {seed_root}/{kind}.conf > "$target/{kind}.conf"; printf '%s\\n' '{digest}' > "$target/.platform-seed-sha256"; printf '%s\\n' "$operation" > "$target/.platform-install-operation")
else
  test ! -L "$target"; test -d "$target"
  test ! -L "$target/.platform-seed-sha256"; test -f "$target/.platform-seed-sha256"
  test "$(cat "$target/.platform-seed-sha256")" = '{digest}'
  test ! -L "$target/.platform-install-operation"; test -f "$target/.platform-install-operation"
  test "$(cat "$target/.platform-install-operation")" = "$operation"
  test ! -L "$target/{kind}.conf"; test -f "$target/{kind}.conf"
fi''' for kind,digest in (("server",server_hash),("sentinel",sentinel_hash))]])+"\n"


def kubernetes(plan):
    ns=plan["namespace"]; objects=[]
    for member in plan["members"]:
        name=member["name"]; labels={**LABELS,"platform.gitops/member":name}
        configs={kind+".conf":configuration(plan,member,"k8s",kind) for kind in ("server","sentinel")}
        hashes={k:hashlib.sha256(v.encode()).hexdigest() for k,v in configs.items()}; cfg=name+"-seed-"+hashlib.sha256(json.dumps(configs,sort_keys=True).encode()).hexdigest()[:12]
        meta={"name":name,"namespace":ns,"labels":labels}
        objects += [{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":cfg,"namespace":ns},"immutable":True,"data":configs},
            {"apiVersion":"v1","kind":"Service","metadata":meta,"spec":{"clusterIP":"None","publishNotReadyAddresses":True,"selector":labels,
                "ports":[{"name":"data-tls","port":6380,"targetPort":6380},{"name":"sentinel-tls","port":26380,"targetPort":26380}]}}]
        common={"image":plan["image"],"securityContext":{"allowPrivilegeEscalation":False,"readOnlyRootFilesystem":True,"capabilities":{"drop":["ALL"]}}}
        containers=[]
        for kind,port in (("server",6380),("sentinel",26380)):
            memory=plan["settings"]["maxmemory_mib"]*2+256 if kind=="server" else 128
            containers.append({**common,"name":kind,"command":[*ENTRYPOINT,"/state/"+kind+"/"+kind+".conf",*(["--sentinel"] if kind=="sentinel" else [])],
                "ports":[{"name":"data-tls" if kind=="server" else "sentinel-tls","containerPort":port}],"resources":{"requests":{"cpu":"100m","memory":"64Mi"},"limits":{"cpu":"2","memory":str(memory)+"Mi"}},
                "startupProbe":{"tcpSocket":{"port":port},"periodSeconds":3,"failureThreshold":60},"livenessProbe":{"tcpSocket":{"port":port},"periodSeconds":10},
                "readinessProbe":{"tcpSocket":{"port":port},"periodSeconds":5},
                "volumeMounts":[{"name":kind,"mountPath":"/state/"+kind,"subPath":"runtime"},{"name":"tls","mountPath":"/run/platform-valkey/tls","readOnly":True},
                    {"name":"auth","mountPath":"/run/platform-valkey/auth","readOnly":True},{"name":"tmp-"+kind,"mountPath":"/tmp"}]})
        pod={"automountServiceAccountToken":False,"terminationGracePeriodSeconds":60,"securityContext":{"runAsNonRoot":True,"runAsUser":1000,"runAsGroup":1000,"fsGroup":1000,"seccompProfile":{"type":"RuntimeDefault"}},
            "affinity":{"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"kubernetes.io/hostname","operator":"In","values":[member["node"]]},
                {"key":"topology.kubernetes.io/zone","operator":"In","values":[member["failure_domain"]]}]}]}},
                "podAntiAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":[{"labelSelector":{"matchLabels":LABELS},"topologyKey":"kubernetes.io/hostname"}]}},
            "initContainers":[{"name":"seed-original-config","image":plan["tools_image"],"command":["/bin/sh","-c",seed_script(hashes["server.conf"],hashes["sentinel.conf"])],
                "env":[{"name":"VALKEY_INSTALL_OPERATION_ID","valueFrom":{"fieldRef":{"apiVersion":"v1","fieldPath":"metadata.annotations['platform.gitops/valkey-operation']"}}}],
                "securityContext":{"allowPrivilegeEscalation":False,"readOnlyRootFilesystem":True,"capabilities":{"drop":["ALL"]}},"resources":{"requests":{"cpu":"10m","memory":"16Mi"},"limits":{"cpu":"100m","memory":"32Mi"}},
                "volumeMounts":[{"name":"seed","mountPath":"/seed","readOnly":True},*[{"name":k,"mountPath":"/volumes/"+k} for k in ("server","sentinel")]]}],"containers":containers,
            "volumes":[{"name":"seed","configMap":{"name":cfg}},{"name":"tls","secret":{"secretName":member["tls_secret"],"defaultMode":0o440,"items":[{"key":k,"path":k} for k in ("tls.crt","tls.key","ca.crt")]}},
                {"name":"auth","secret":{"secretName":member["auth_secret"],"defaultMode":0o440,"items":[{"key":k,"path":k} for k in ("server-auth.conf","server.acl","sentinel-auth.conf","sentinel.acl")]}},
                *[{"name":k,"persistentVolumeClaim":{"claimName":member[k+"_claim"]}} for k in ("server","sentinel")],
                *[{"name":"tmp-"+k,"emptyDir":{"medium":"Memory","sizeLimit":"64Mi"}} for k in ("server","sentinel")]]}
        objects.append({"apiVersion":"apps/v1","kind":"Deployment","metadata":meta,"spec":{"replicas":1,"strategy":{"type":"Recreate"},"selector":{"matchLabels":labels},"template":{"metadata":{"labels":labels},"spec":pod}}})
    objects.append({"apiVersion":"policy/v1","kind":"PodDisruptionBudget","metadata":{"name":"platform-valkey-sentinel","namespace":ns},"spec":{"minAvailable":2,"selector":{"matchLabels":LABELS}}})
    peer={"podSelector":{"matchLabels":LABELS}}; ports=[{"protocol":"TCP","port":p} for p in (6380,26380)]
    clients=[{"namespaceSelector":{"matchLabels":{"kubernetes.io/metadata.name":n}}} for n in plan["network"]["client_namespaces"]]
    objects.append({"apiVersion":"networking.k8s.io/v1","kind":"NetworkPolicy","metadata":{"name":"platform-valkey-sentinel","namespace":ns},"spec":{"podSelector":{"matchLabels":LABELS},"policyTypes":["Ingress","Egress"],
        "ingress":[{"from":[peer,*clients],"ports":ports}],"egress":[{"to":[peer],"ports":ports},{"to":[{"namespaceSelector":{"matchLabels":{"kubernetes.io/metadata.name":"kube-system"}},"podSelector":{"matchLabels":{"k8s-app":"kube-dns"}}}],"ports":[{"protocol":p,"port":53} for p in ("TCP","UDP")]}]}})
    return objects


def render(plan,runtime):
    validate(plan)
    if runtime not in RUNTIMES: raise ValueError("Unknown Valkey runtime")
    files={}
    if runtime in {"rke2","k3s","k8s"}: files["valkey.yaml"]=yaml.safe_dump_all(kubernetes(plan),sort_keys=False)
    else:
        for member in plan["members"]:
            name=member["name"]; private,server,sentinel=locations(plan,member,runtime)
            for kind in ("server","sentinel"):
                files[name+"-"+kind+".conf"]=configuration(plan,member,runtime,kind)
                state=server if kind=="server" else sentinel; suffix=["--sentinel"] if kind=="sentinel" else []
                if runtime=="raw":
                    args=[plan["binary"],state+"/"+kind+".conf",*suffix]; memory=plan["settings"]["maxmemory_mib"]*2+256 if kind=="server" else 128
                    files[name+"-"+kind+".service"]="\n".join(["[Unit]","Description=Valkey "+kind+" "+name,"After=network-online.target","Wants=network-online.target","", "[Service]","Type=simple","User=valkey","Group=valkey",
                        "ExecStartPre=/usr/bin/sha256sum --check --strict "+private+"/binary.sha256","ExecStart="+" ".join(shlex.quote(v) for v in args),"Restart=on-failure","RestartSec=5","TimeoutStopSec=60",
                        "NoNewPrivileges=yes","ProtectSystem=strict","ProtectHome=yes","PrivateTmp=yes","PrivateDevices=yes","CapabilityBoundingSet=","UMask=0077","MemoryMax="+str(memory)+"M","MemorySwapMax=0","TasksMax=256","ReadWritePaths="+state,"","[Install]","WantedBy=multi-user.target",""])
                else:
                    mounts=[(member[k+"_dir"],"/state/"+k,False) for k in ("server","sentinel") if k==kind]+[(plan["private_dir"]+"/"+name+"/"+k,"/run/platform-valkey/"+k,True) for k in ("tls","auth")]
                    files[name+"-"+kind+"-compose.yaml"]=yaml.safe_dump({"services":{name+"-"+kind:{"image":plan["image"],"entrypoint":[ENTRYPOINT[0]],"command":[*ENTRYPOINT[1:],state+"/"+kind+".conf",*suffix],"user":"1000:1000","network_mode":"host","read_only":True,
                        "restart":"unless-stopped","cap_drop":["ALL"],"security_opt":["no-new-privileges:true"],"pids_limit":256,"mem_limit":str(plan["settings"]["maxmemory_mib"]*2+256 if kind=="server" else 128)+"m","memswap_limit":str(plan["settings"]["maxmemory_mib"]*2+256 if kind=="server" else 128)+"m","cpus":2.0,
                        "tmpfs":["/tmp:rw,nosuid,nodev,noexec,mode=1777,size=64m"],"volumes":[{"type":"bind","source":s,"target":t,"read_only":ro,"bind":{"create_host_path":False}} for s,t,ro in mounts]}}},sort_keys=False)
            if runtime=="raw": files[name+"-binary.sha256"]=plan["binary_sha256"]+"  "+plan["binary"]+"\n"
    files["valkey-contract.json"]=json.dumps({"schema_version":1,"runtime":runtime,"plan":plan,"files_sha256":{n:hashlib.sha256(v.encode()).hexdigest() for n,v in sorted(files.items())}},sort_keys=True,indent=2)+"\n"
    files["render-status.json"]=json.dumps({"component":"valkey-replication-sentinel","runtime":runtime,"rendered":True,"deployed":False,"tls_verified":False,"replication_verified":False,"failover_verified":False,
        "application_compatibility_verified":False,"lossless_failover_verified":False,"physical_node_loss_verified":False,"ha_verified":False,"full_stack_verified":False,
        "remaining":["tls-enabled-artifact-provenance","private-auth-materialization-and-prepared-state","guarded-runtime-activation","sentinel-capable-application-client-integration","asynchronous-loss-window-and-fencing-review","physical-failure-and-upgrade-acceptance","independent-state-restore"]},indent=2)+"\n"
    return files


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--plan",type=Path,required=True); parser.add_argument("--runtime",choices=RUNTIMES,required=True); parser.add_argument("--output-dir",type=Path,required=True); args=parser.parse_args(argv)
    try:
        plan=loads_strict_json(read_bounded_text(args.plan,max_bytes=128*1024)); write_bundle(render(plan,args.runtime),args.output_dir,private_root=ROOT/"private")
        print('{"rendered":true,"deployed":false,"application_compatibility_verified":false,"ha_verified":false}'); return 0
    except Exception:
        print("Valkey rendering stopped; private values suppressed.",file=sys.stderr); return 1


if __name__=="__main__": raise SystemExit(main())
