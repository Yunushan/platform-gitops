#!/usr/bin/env python3
"""Disposable six-process mTLS/Sentinel acceptance with real quorum decisions."""

import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
import materialize_valkey_auth as auth
import render_valkey_ha as renderer
import verify_valkey_runtime as verifier
from test_valkey_ha import fixture,references


BINARY_SHA="7e0ca79f225ef4211c47be7a77513e0ef9e88ab64ce078fb21292658c55eceef"
SOURCE_SHA="db3642dfc2fb24e42313b9e9c1557cf0160acce3bf2cd366ce654ce291133b50"
CLEAN={"PATH":"/usr/bin:/bin","HOME":"/nonexistent","LANG":"C"}


class ReplyError(Exception):
    def __init__(self,raw):
        code=raw.split(b" ",1)[0].decode("ascii",errors="replace")
        self.code=code if code in {"NOAUTH","WRONGPASS","NOPERM","NOREPLICAS","MASTERDOWN","READONLY","LOADING","NOQUORUM"} else "OTHER"
        super().__init__("A fixture cache command was refused; details suppressed")


class RESP:
    def __init__(self,address,port,context,username=None,password=None):
        self.socket=context.wrap_socket(socket.create_connection((address,port),timeout=3),server_hostname=address); self.socket.settimeout(3); self.stream=self.socket.makefile("rb")
        if username is not None:
            try: self.command("AUTH",username,password)
            except BaseException: self.close(); raise
    def close(self): self.stream.close(); self.socket.close()
    def __enter__(self): return self
    def __exit__(self,*args): self.close()
    def command(self,*args):
        values=[v if isinstance(v,bytes) else str(v).encode() for v in args]
        if not 1<=len(values)<=32 or any(len(v)>8192 for v in values): raise ValueError("Bound fixture request")
        self.socket.sendall(b"*"+str(len(values)).encode()+b"\r\n"+b"".join(b"$"+str(len(v)).encode()+b"\r\n"+v+b"\r\n" for v in values)); self.remaining=512*1024
        return self.read(0)
    def read(self,depth):
        if depth>4: raise ValueError("Bound RESP nesting")
        line=self.stream.readline(4097); self.remaining-=len(line)
        if not line.endswith(b"\r\n") or len(line)>4096 or self.remaining<0: raise ValueError("Invalid bounded RESP line")
        kind,value=line[:1],line[1:-2]
        if kind==b"-": raise ReplyError(value)
        if kind==b"+": return value.decode()
        if kind==b":": return int(value)
        count=int(value)
        if kind==b"$":
            if count==-1: return None
            if not 0<=count<=self.remaining-2: raise ValueError("Bound RESP bulk body")
            data=self.stream.read(count+2); self.remaining-=len(data)
            if len(data)!=count+2 or not data.endswith(b"\r\n"): raise ValueError("Incomplete RESP bulk body")
            return data[:-2].decode()
        if kind==b"*" and 0<=count<=256: return [self.read(depth+1) for _ in range(count)]
        raise ValueError("Unexpected fixture RESP type")


def pinned_test_binary():
    inputs=Path(os.environ["VALKEY_TEST_ARTIFACTS"]).resolve(strict=True); binary=inputs/"valkey-server"
    if binary.is_symlink() or not 0<binary.stat().st_size<128*1024**2: raise ValueError("Use the bounded pinned TLS executable")
    with binary.open("rb") as stream:
        if hashlib.file_digest(stream,"sha256").hexdigest()!=BINARY_SHA: raise ValueError("The TLS test build changed")
    build=json.loads(read_bounded_bytes(inputs/"build-inputs.json",max_bytes=16*1024))
    if build.get("binary_sha256")!=BINARY_SHA or build.get("source_sha256")!=SOURCE_SHA or "BUILD_TLS=yes" not in build.get("options",[]): raise ValueError("The pinned TLS build inputs differ")
    for name,digest in build.get("runtime_libraries_sha256",{}).items():
        if name not in {"libssl.so.3","libcrypto.so.3"} or hashlib.sha256(Path("/usr/lib/x86_64-linux-gnu",name).read_bytes()).hexdigest()!=digest: raise ValueError("The test TLS runtime changed")
    return binary


def exercise(test,*,application=None,worker_context=None):
    if sys.platform!="linux" or os.geteuid()!=0 or os.environ.get("VALKEY_TEST_RUNTIME")!="1": raise ValueError("Select the explicit disposable root Linux TLS fixture")
    if Path("/proc/self/ns/net").stat().st_ino==Path("/proc/1/ns/net").stat().st_ino: raise ValueError("Use a fresh isolated namespace")
    source_directory=Path(__file__).resolve().parent
    source_paths={Path(m.__file__).resolve() for m in list(sys.modules.values()) if getattr(m,"__file__",None) and Path(m.__file__).suffix==".py" and Path(m.__file__).resolve().parent==source_directory}
    commitment={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source_paths)}
    binary=pinned_test_binary()
    root=Path(tempfile.mkdtemp(prefix="platform-valkey-runtime-",dir="/tmp")); root.chmod(0o755); identity=(root.stat().st_dev,root.stat().st_ino)
    if application is not None and any(not callable(getattr(application,name,None)) for name in ("configure","auth_files","exercise")): raise ValueError("Select one explicit application acceptance callback")
    duration = getattr(application, "application_timeout_seconds", 600) if application is not None else 220
    if type(duration) is not int or not 220 <= duration <= 3500: raise ValueError("Use a finite reviewed application deadline")
    account=pwd.getpwnam("nobody"); processes={}; logs={}; stopped=set(); private_values=[]; stage="preparation"; deadline=time.monotonic()+duration; report={}
    def run(args):
        result=run_bounded(list(map(str,args)),env=CLEAN,input="",text=True,timeout=30,output_max_bytes=256*1024,check=False)
        if result.returncode: raise ValueError("A private fixture setup command failed")
        return result.stdout
    def write(name,value,*,owner=False,mode=0o644):
        path=root/name; path.parent.mkdir(parents=True,exist_ok=True)
        for p in [*reversed(path.parent.parents),path.parent]:
            if p.is_relative_to(root): p.chmod(0o755)
        path.write_text(value,encoding="utf-8",newline="\n"); path.chmod(mode)
        if owner: os.chown(path,account.pw_uid,account.pw_gid)
        return path
    def start(m,kind):
        label=m["name"]+"-"+kind
        if label in processes and processes[label].poll() is None: raise ValueError("Never replace a live original fixture process")
        log=root/(label+"-"+str(len(logs))+".log")
        with log.open("xb") as output:
            child=subprocess.Popen([str(root/"valkey-server"),m[kind+"_dir"]+"/"+kind+".conf",*(["--sentinel"] if kind=="sentinel" else [])],cwd=root,env=CLEAN,stdout=output,stderr=subprocess.STDOUT,
                user=account.pw_uid,group=account.pw_gid,extra_groups=[],start_new_session=True)
        processes[label]=child; logs[log]=child; return child
    def stop(m,kind):
        child=processes[m["name"]+"-"+kind]
        if child.poll() is not None: raise ValueError("An original process exited before the deliberate loss")
        stopped.add(child.pid); child.kill(); child.wait(timeout=10)
    def spawn_application(label,args,environment,*,root_setup=False):
        if label in processes: raise ValueError("Retain every original application process")
        log=root/(label+".log")
        with log.open("xb") as output:
            identity = {} if root_setup else {"user":account.pw_uid, "group":account.pw_gid, "extra_groups":[]}
            child=subprocess.Popen(list(map(str,args)),cwd=root,env=environment,stdout=output,stderr=subprocess.STDOUT,**identity,start_new_session=True)
        processes[label]=child; logs[log]=child; return child
    def stop_application(child):
        if child not in processes.values(): raise ValueError("Stop only an original fixture application")
        stopped.add(child.pid)
        if child.poll() is None:
            child.terminate()
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=10)
    def wait(check,seconds=45):
        until=min(deadline,time.monotonic()+seconds)
        while time.monotonic()<until:
            for label, child in processes.items():
                if child.poll() not in {None,0} and child.pid not in stopped:
                    raise ValueError(f"Fixture process {label} exited unexpectedly with status {child.returncode}")
            try:
                result=check()
                if result: return result
            except ssl.SSLCertVerificationError: raise ValueError("Fixture certificate verification failed") from None
            except ReplyError as error:
                if error.code not in {"MASTERDOWN","LOADING","NOREPLICAS"}: raise
            except (OSError,TimeoutError): pass
            time.sleep(0.2)
        raise TimeoutError("The original Valkey/Sentinel observation did not converge")
    added_addresses=[]
    try:
        links=json.loads(run(["/usr/sbin/ip","-j","address","show"]))
        if worker_context is not None:
            from test_gerrit_review_runtime_fixture import namespace
            namespace(worker_context)
            if application is None: raise ValueError("The worker fixture requires an application acceptance callback")
        elif len(links)!=1 or links[0]["ifname"]!="lo" or links[0].get("addr_info"):
            raise ValueError("The fixture network is not fresh")
        run(["/usr/sbin/ip","link","set","lo","up"]); shutil.copyfile(binary,root/"valkey-server"); (root/"valkey-server").chmod(0o755)
        plan=fixture(); plan.update(private_dir=str(root/"inputs"),binary=str(root/"valkey-server"),binary_sha256=BINARY_SHA)
        plan["settings"].update(maxmemory_mib=64,max_lag_seconds=1,down_after_ms=2000,failover_timeout_ms=15000)
        if application is not None: application.configure(plan)
        env={v:secrets.token_urlsafe(36) for v in references().values()}; private_values.extend(env.values())
        diagnostic=secrets.token_urlsafe(36); private_values.append(diagnostic)
        run(["/usr/bin/openssl","req","-x509","-newkey","ec","-pkeyopt","ec_paramgen_curve:P-256","-nodes","-days","1","-addext","basicConstraints=critical,CA:TRUE","-addext","keyUsage=critical,keyCertSign,cRLSign","-subj","/CN=fixture-valkey-ca","-keyout",root/"ca.key","-out",root/"ca.crt"])
        (root/"ca.key").chmod(0o600)
        def certificate(name,address,*,client=False):
            tls=root/"inputs"/name/"tls"; tls.mkdir(parents=True)
            for p in [root/"inputs",tls.parent,tls]: p.chmod(0o755)
            dns_names = application.certificate_dns_names(name) if application is not None and hasattr(application, "certificate_dns_names") and not client else []
            if not isinstance(dns_names, list) or len(dns_names) > 3 or any(not renderer.dns_name(value) or not re.search(r"[A-Za-z]", value) for value in dns_names): raise ValueError("Bound optional fixture DNS identities")
            extension=write(name+"-extensions.cnf",("subjectAltName=DNS:fixture-client\nextendedKeyUsage=clientAuth\n" if client else "subjectAltName=IP:"+address+"".join(",DNS:"+value for value in dns_names)+"\nextendedKeyUsage=serverAuth,clientAuth\n")+
                "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\n")
            run(["/usr/bin/openssl","req","-new","-newkey","ec","-pkeyopt","ec_paramgen_curve:P-256","-nodes","-subj","/CN="+name,"-keyout",tls/"tls.key","-out",root/(name+".csr")])
            run(["/usr/bin/openssl","x509","-req","-in",root/(name+".csr"),"-CA",root/"ca.crt","-CAkey",root/"ca.key","-CAcreateserial","-days","1","-extfile",extension,"-out",tls/"tls.crt"])
            shutil.copyfile(root/"ca.crt",tls/"ca.crt")
            for name in ("ca.crt", "tls.crt", "tls.key"):
                file=tls/name; file.chmod(0o440); os.chown(file,0,account.pw_gid)
            return tls
        for m in plan["members"]:
            run(["/usr/sbin/ip","address","add",m["address"]+"/32","dev","lo"])
            added_addresses.append(m["address"])
            certificate(m["name"],m["address"])
            for kind in ("server","sentinel"):
                state=root/(m["name"]+"-"+kind); state.mkdir(); state.chmod(0o700); os.chown(state,account.pw_uid,account.pw_gid); m[kind+"_dir"]=str(state)
        renderer.validate(plan)
        for m in plan["members"]:
            prepared_auth=auth.files(plan,"raw",m["name"],references(),environment=env) if application is None else application.auth_files(plan,"raw",m["name"],references(),environment=env)
            for n,v in prepared_auth.items():
                if n=="server.acl": v += "user fixture_diagnostics reset on #"+hashlib.sha256(diagnostic.encode()).hexdigest()+" resetchannels -@all +acl|log"+(" +scan" if application is not None else "")+"\n"
                p=write("inputs/"+m["name"]+"/auth/"+n,v,mode=0o440); os.chown(p,0,account.pw_gid)
            for kind in ("server","sentinel"):
                path=Path(m[kind+"_dir"])/(kind+".conf"); path.write_text(renderer.configuration(plan,m,"raw",kind)); path.chmod(0o600); os.chown(path,account.pw_uid,account.pw_gid)
                start(m,kind)
        client_tls=certificate("client","",client=True); context=ssl.create_default_context(cafile=str(root/"ca.crt")); context.load_cert_chain(client_tls/"tls.crt",client_tls/"tls.key")
        def connection(m,kind="server",*,authenticate=True):
            role="probe" if kind=="server" else "discovery"
            return RESP(m["address"],6380 if kind=="server" else 26380,context,role if authenticate else None,env[references()[role]] if authenticate else None)
        def command(m,*args,kind="server"):
            with connection(m,kind) as client: return client.command(*args)
        def info(m): return dict(line.split(":",1) for line in command(m,"INFO","replication").splitlines() if line and not line.startswith("#") and ":" in line)
        def agreed(sentinels,*,except_ip=None):
            addresses=[command(m,"SENTINEL","GET-MASTER-ADDR-BY-NAME",plan["cache_name"],kind="sentinel") for m in sentinels]
            if any(not isinstance(a,list) or len(a)!=2 or a[1]!="6380" for a in addresses) or any(a!=addresses[0] for a in addresses): return False
            if except_ip is not None and addresses[0][0]==except_ip: return False
            match=next((m for m in plan["members"] if m["address"]==addresses[0][0]),None)
            return match if match is not None and command(match,"ROLE")[0]=="master" else False
        def replicated(primary,count): return int(info(primary).get("connected_slaves","0"))>=count
        def write_witness(primary,key,value,count):
            with connection(primary) as client:
                if client.command("SET",key,value)!="OK" or client.command("WAIT",count,5000)<count: return False
                return client.command("GET",key)==value
        stage="initial-tls-replication"
        for m in plan["members"]:
            wait(lambda:command(m,"PING")=="PONG"); wait(lambda:command(m,"PING",kind="sentinel")=="PONG")
        first=next(m for m in plan["members"] if m["name"]==plan["primary"]); wait(lambda:replicated(first,2))
        def replicas_discovered(m):
            rows=command(m,"SENTINEL","REPLICAS",plan["cache_name"],kind="sentinel")
            if not isinstance(rows,list) or len(rows)!=2: return False
            entries=[dict(zip(row[::2],row[1::2])) for row in rows]
            return ({row.get("ip") for row in entries}=={p["address"] for p in plan["members"] if p!=first}
                and all(row.get("master-link-status")=="ok" and not {"s_down","o_down","disconnected"}.intersection(row.get("flags","").split(",")) for row in entries))
        for m in plan["members"]:
            wait(lambda:len(command(m,"SENTINEL","SENTINELS",plan["cache_name"],kind="sentinel"))==2)
            wait(lambda:command(m,"SENTINEL","CKQUORUM",plan["cache_name"],kind="sentinel").startswith("OK"))
            wait(lambda:replicas_discovered(m))
            test.assertEqual(command(m,"SENTINEL","GET-MASTER-ADDR-BY-NAME",plan["cache_name"],kind="sentinel"),[first["address"],"6380"])
        witness=secrets.token_urlsafe(36); private_values.append(witness); wait(lambda:write_witness(first,"platform-probe:witness",witness,2))
        for m in plan["members"]: wait(lambda:command(m,"GET","platform-probe:witness")==witness)
        for args in (("GET","unreviewed:key"),("CONFIG","GET","*"),("SET","unreviewed:key","denied")):
            with test.assertRaises(ReplyError) as raised: command(first,*args)
            test.assertEqual(raised.exception.code,"NOPERM")
        with connection(first,authenticate=False) as client:
            with test.assertRaises(ReplyError) as raised: client.command("GET","platform-probe:witness")
            test.assertEqual(raised.exception.code,"NOAUTH")
        with test.assertRaises(ssl.SSLCertVerificationError):
            with socket.create_connection((first["address"],6380),timeout=3) as raw: context.wrap_socket(raw,server_hostname="wrong.example.invalid")
        no_client=ssl.create_default_context(cafile=str(root/"ca.crt"))
        with test.assertRaises((ssl.SSLError,OSError)):
            with RESP(first["address"],6380,no_client) as client: client.command("PING")
        report.update(tls_replication_verified=True,restricted_data_scope_verified=True,sentinel_discovery_verified=True,sentinel_replica_inventory_converged=True,untrusted_hostname_refused=True,missing_client_certificate_refused=True)
        bundle=root/"verification-bundle"
        for name,value in renderer.render(plan,"raw").items(): write("verification-bundle/"+name,value)
        def runtime_plan():
            trust={}
            for kind,path in (("ca",root/"ca.crt"),("client_cert",client_tls/"tls.crt"),("client_key",client_tls/"tls.key")):
                trust[kind+"_file"]=str(path); trust[kind+"_sha256"]=hashlib.sha256(path.read_bytes()).hexdigest()
            pins={}
            for m in plan["members"]:
                server=verifier.info(command(m,"INFO","server")); cert=(root/"inputs"/m["name"]/"tls/tls.crt").read_text()
                pins[m["name"]]={"connect_address":m["address"],"addresses":[m["address"]],"server_run_id":server["run_id"],
                    "sentinel_id":command(m,"SENTINEL","MYID",kind="sentinel"),"server_cert_sha256":hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert)).hexdigest()}
            return {"schema_version":1,"contract_sha256":hashlib.sha256((bundle/"valkey-contract.json").read_bytes()).hexdigest(),"trust":trust,
                "credentials":{"probe_env":references()["probe"],"discovery_env":references()["discovery"]},"members":pins,
                "limits":{"request_seconds":5,"operation_seconds":60,"max_response_bytes":524288,"canary_ttl_seconds":180,"replication_wait_ms":3000}}
        # Use synthetic scoped credentials only during the production helper call.
        from unittest.mock import patch
        original_runtime_plan=runtime_plan(); stage="idle-replication-freshness"
        # INFO/PING reads must not conceal a long replication heartbeat gap.
        # Retain the one-second freshness policy through a quiet interval longer
        # than that policy before performing either production-verifier call.
        quiet_until=time.monotonic()+plan["settings"]["max_lag_seconds"]+2
        while time.monotonic()<quiet_until:
            for m in plan["members"]:
                if m!=first and int(info(m).get("master_last_io_seconds_ago","-1")) not in range(plan["settings"]["max_lag_seconds"]+1):
                    raise ValueError("An idle replica exceeded the original freshness policy")
            time.sleep(0.2)
        report["idle_replication_freshness_verified"]=True; stage="production-runtime-verifier"
        with patch.dict(os.environ,env):
            readonly=verifier.operate(bundle,original_runtime_plan,live=True)
            canary=verifier.operate(bundle,original_runtime_plan,live=True,canary=True)
        test.assertTrue(readonly["live_observations_verified"]); test.assertFalse(readonly["canary_replication_verified"])
        test.assertTrue(canary["canary_replication_verified"])
        report.update(runtime_verifier_read_only_verified=True,runtime_verifier_canary_verified=True,runtime_verifier_toolset_sha256=readonly["toolset_sha256"])
        promoted=None
        def primary_loss():
            nonlocal stage,promoted
            if promoted is not None: raise ValueError("Inject primary loss exactly once")
            stage="primary-and-colocated-sentinel-loss"; stop(first,"server"); stop(first,"sentinel"); surviving=[m for m in plan["members"] if m!=first]
            promoted=wait(lambda:agreed(surviving,except_ip=first["address"]),seconds=60); wait(lambda:replicated(promoted,1))
            test.assertEqual(command(promoted,"GET","platform-probe:witness"),witness)
            wait(lambda:write_witness(promoted,"platform-probe:after-loss",witness,1)); report["primary_process_loss_failover_verified"]=True
            return promoted
        if application is None: primary_loss()
        else:
            stage="application-client-acceptance"
            extra=application.exercise({"test":test,"root":root,"plan":plan,"bundle":bundle,"tls":client_tls,"tls_context":context,"environment":env,"references":references(),"run":run,"write":write,
                "wait":wait,"command":command,"primary_loss":primary_loss,"spawn":spawn_application,"stop":stop_application,"private_values":private_values,"diagnostic":diagnostic,"runtime_plan":original_runtime_plan})
            prefix = getattr(application, "evidence_prefix", "argocd_")
            if prefix not in {"argocd_", "valkey_gateway_"} or not isinstance(extra,dict) or any(not k.startswith(prefix) or type(v) is not bool and not (isinstance(v,dict) and all(isinstance(h,str) and re.fullmatch(r"[0-9a-f]{64}",h) for h in v.values())) for k,v in extra.items()): raise ValueError("Application acceptance must return bounded aggregate evidence")
            report.update(extra)
            if promoted is None: raise ValueError("Application acceptance omitted the real primary-loss case")
        stage="original-member-rejoin"; start(first,"server"); start(first,"sentinel")
        wait(lambda:command(first,"ROLE")[0]=="slave" and info(first).get("master_link_status")=="up")
        wait(lambda:command(first,"GET","platform-probe:after-loss")==witness); wait(lambda:replicated(promoted,2))
        def rejoined():
            found=agreed(plan["members"])
            return bool(found) and found["name"]==promoted["name"]
        wait(rejoined)
        report["original_state_rejoin_verified"]=True
        stage="production-verifier-after-rejoin"
        # Sentinel metadata converges asynchronously after the original member restarts.
        current_runtime_plan=runtime_plan()
        with patch.dict(os.environ,env):
            with test.assertRaisesRegex(ValueError,"invocation"):
                verifier.operate(bundle,original_runtime_plan,live=True)
            def observe_rejoined():
                try: return verifier.operate(bundle,current_runtime_plan,live=True)
                except ValueError: return False
            observed=wait(observe_rejoined,seconds=30)
        test.assertTrue(observed["live_observations_verified"])
        report.update(runtime_verifier_rejoined_topology_verified=True,runtime_verifier_changed_invocation_refused=True)
        stage="sentinel-majority-loss"
        for m in plan["members"][:2]: stop(m,"sentinel")
        sole=plan["members"][2]; stop(promoted,"server")
        until=time.monotonic()+plan["settings"]["down_after_ms"]/1000*2+2
        while time.monotonic()<until:
            for m in plan["members"]:
                if m!=promoted: test.assertEqual(command(m,"ROLE")[0],"slave")
            time.sleep(0.2)
        with test.assertRaises(ReplyError) as raised: command(sole,"SENTINEL","CKQUORUM",plan["cache_name"],kind="sentinel")
        test.assertEqual(raised.exception.code,"NOQUORUM"); report["sentinel_quorum_loss_refused"]=True
        stage="recovered-majority-promotion"; restored=plan["members"][0]; start(restored,"sentinel")
        recovered=wait(lambda:agreed([sole,restored],except_ip=promoted["address"]),seconds=60); wait(lambda:replicated(recovered,1))
        test.assertEqual(command(recovered,"GET","platform-probe:witness"),witness); wait(lambda:write_witness(recovered,"platform-probe:after-quorum",witness,1))
        report["recovered_quorum_failover_verified"]=True
        replica=next(m for m in plan["members"] if m not in (recovered,promoted)); stop(replica,"server")
        def guarded():
            try: command(recovered,"SET","platform-probe:write-guard",witness)
            except ReplyError as error:
                if error.code=="NOREPLICAS": return True
                raise
            return False
        wait(guarded,seconds=10); report["isolated_primary_write_guard_verified"]=True
        for n,h in commitment.items():
            if hashlib.sha256((source_directory/n).read_bytes()).hexdigest()!=h: raise ValueError("Acceptance sources changed")
        report.update(version=plan["version"],source_sha256=commitment,binary_sha256=BINARY_SHA,source_archive_sha256=SOURCE_SHA,
            publisher_signature_verified=False,application_compatibility_verified=False,lossless_failover_verified=False,physical_node_loss_verified=False,ha_verified=False,full_stack_verified=False)
    except BaseException:
        events={}; denied_commands=set(); startup_signals=set()
        for path in logs:
            data=read_bounded_bytes(path,max_bytes=2*1024**2).decode(errors="replace")
            for signal in ("permission denied", "fatal config", "error loading", "failed to load", "address already in use"):
                if signal in data.lower(): startup_signals.add(signal)
            for event in re.findall(r" ([+-][a-z][a-z-]{1,49})(?: |\r?$)",data,re.M): events[event]=events.get(event,0)+1
        if "context" in locals():
            for m in plan["members"]:
                child=processes.get(m["name"]+"-server")
                if child is None or child.poll() is not None: continue
                try:
                    with RESP(m["address"],6380,context,"fixture_diagnostics",diagnostic) as client: rows=client.command("ACL","LOG",20)
                    for row in rows:
                        entry=dict(zip(row[::2],row[1::2]))
                        if entry.get("reason")=="command" and isinstance(entry.get("object"),str) and re.fullmatch(r"[a-z][a-z0-9| -]{0,60}",entry["object"]): denied_commands.add(entry["object"])
                except Exception: pass
        print(json.dumps({"valkey_fixture_stage":stage,"private_details_suppressed":True,"log_signals":{"error_lines":sum(read_bounded_bytes(p,max_bytes=2*1024**2).lower().count(b"error") for p in logs),
            "acl_lines":sum(read_bounded_bytes(p,max_bytes=2*1024**2).lower().count(b"permission") for p in logs)},"startup_signals":sorted(startup_signals),"sentinel_event_counts":events,"denied_commands":sorted(denied_commands)}),flush=True); raise
    finally:
        for child in processes.values():
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=10)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=10)
        for address in reversed(added_addresses):
            run(["/usr/sbin/ip","address","del",address+"/32","dev","lo"])
        for path in logs:
            data=read_bounded_bytes(path,max_bytes=2*1024**2)
            if any(value.encode() in data for value in private_values): raise ValueError("Private fixture data appeared in server logs")
        if root.is_symlink() or root.parent!=Path("/tmp") or not root.name.startswith("platform-valkey-runtime-") or (root.stat().st_dev,root.stat().st_ino)!=identity: raise ValueError("The original cleanup scope changed")
        shutil.rmtree(root)
    report.update(temporary_processes_and_data_removed=True,private_values_absent_from_logs=True)
    output=os.environ.get("VALKEY_TEST_REPORT")
    if output:
        target=Path(output)
        with target.open("x") as stream: os.chmod(target,0o600); json.dump(report,stream,sort_keys=True,indent=2); stream.write("\n")
    return report
