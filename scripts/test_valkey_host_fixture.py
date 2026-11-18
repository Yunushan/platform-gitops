#!/usr/bin/env python3
"""Actual native pair activation in a disposable network namespace/systemd units."""

import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import ssl
import sys
import tempfile
import time
from unittest import mock
from uuid import uuid4

import activate_valkey_host as activation
from bounded_subprocess import run_bounded
import materialize_valkey_auth as auth
import render_valkey_ha as renderer
from test_valkey_ha import fixture,references
from test_valkey_host import host_plan
from test_valkey_runtime_fixture import BINARY_SHA,SOURCE_SHA,RESP
import verify_valkey_runtime as verifier


def exercise(test,*,engine=None):
    runtime="raw" if engine is None else "docker"; switch="VALKEY_TEST_HOST" if engine is None else "VALKEY_TEST_DOCKER_HOST"
    if sys.platform!="linux" or os.geteuid()!=0 or os.environ.get(switch)!="1" or not Path("/run/systemd/system").is_dir(): raise ValueError("Select the explicit disposable host fixture")
    if Path("/proc/self/ns/net").stat().st_ino==Path("/proc/1/ns/net").stat().st_ino: raise ValueError("Use a fresh isolated network namespace")
    source_dir=Path(__file__).resolve().parent; commitment={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source_dir.glob("*.py")}
    parent=Path("/run") if engine is None else engine["root"]; parent.chmod(0o755) if engine is not None else None
    root=Path(tempfile.mkdtemp(prefix="platform-valkey-host-",dir=parent)); root.chmod(0o755); inode=(root.stat().st_dev,root.stat().st_ino)
    token=uuid4().hex[:12]; account=pwd.getpwnam("nobody"); group=grp.getgrgid(account.pw_gid); plans={}; units={}; stage="preparation"; report={}
    env={v:secrets.token_urlsafe(36) for v in references().values()}; service=activation.Host(); service_uid=account.pw_uid if engine is None else 1000; service_gid=account.pw_gid if engine is None else 1000
    def run(args,*,seconds=60,failure=False):
        result=run_bounded(list(map(str,args)),env={**activation.mechanics.CLEAN,**env},input="",text=True,timeout=seconds,output_max_bytes=256*1024,check=False)
        if result.returncode and not failure: raise ValueError("An isolated native fixture command failed")
        return result
    def write(name,value,mode=0o644):
        path=root/name; path.parent.mkdir(parents=True,exist_ok=True)
        for parent in [*reversed(path.parent.parents),path.parent]:
            if parent.is_relative_to(root): parent.chmod(0o755)
        path.write_text(value,encoding="utf-8",newline="\n"); path.chmod(mode); return path
    def worker(name,mode,*,failure=False):
        result=run(["/usr/bin/python3","-B",root/"scripts/host_worker.py",root/(name+"-plan.json"),root/"bundle",mode,root/"private"/name],seconds=180,failure=True)
        if failure:
            if result.returncode!=1: raise ValueError("A required native refusal did not occur")
            return None
        if result.returncode:
            try: diagnostic=json.loads(result.stdout)
            except ValueError: diagnostic={"worker_failed":True}
            print(json.dumps({"valkey_host_safe_failure":diagnostic}),flush=True); raise ValueError("The actual host worker failed")
        return json.loads(result.stdout)
    try:
        rows=json.loads(run(["/usr/sbin/ip","-j","address","show"]).stdout)
        if len(rows)!=1 or rows[0]["ifname"]!="lo" or engine is None and rows[0].get("addr_info"): raise ValueError("The namespace is not fresh")
        run(["/usr/sbin/ip","link","set","lo","up"])
        artifacts=Path(os.environ["VALKEY_TEST_ARTIFACTS"]); binary=artifacts/"valkey-server"; build=json.loads((artifacts/"build-inputs.json").read_text())
        if binary.is_symlink() or not 0<binary.stat().st_size<128*1024**2: raise ValueError("Select the bounded TLS fixture binary")
        with binary.open("rb") as stream: binary_digest=hashlib.file_digest(stream,"sha256").hexdigest()
        if binary_digest!=BINARY_SHA or build.get("binary_sha256")!=BINARY_SHA or build.get("source_sha256")!=SOURCE_SHA or "BUILD_TLS=yes" not in build.get("options",[]): raise ValueError("The reviewed TLS binary changed")
        for lib,digest in build["runtime_libraries_sha256"].items():
            if lib not in {"libssl.so.3","libcrypto.so.3"} or hashlib.sha256(Path("/usr/lib/x86_64-linux-gnu",lib).read_bytes()).hexdigest()!=digest: raise ValueError("The reviewed TLS runtime changed")
        shutil.copyfile(binary,root/"valkey-server"); (root/"valkey-server").chmod(0o755)
        image=None
        if engine is not None:
            from test_valkey_docker_host_fixture import prepare_image
            image=prepare_image(root,engine,root/"valkey-server")
        (root/"scripts").mkdir(mode=0o755)
        for path in source_dir.glob("*.py"):
            target=root/"scripts"/path.name; shutil.copyfile(path,target); target.chmod(0o644)
        worker_source='''import json,sys,traceback
from pathlib import Path
import activate_valkey_host as activation
original=activation.unit_text
def scoped(contract,plan,state,kind):
 return original(contract,plan,state,kind)+"\\n[Service]\\nNetworkNamespacePath=NAMESPACE\\n"
activation.unit_text=scoped
try:
 plan=json.loads(Path(sys.argv[1]).read_text())
 result=activation.operate(Path(sys.argv[2]),plan,mode=sys.argv[3],output=Path(sys.argv[4]),wait_seconds=45)
 print(json.dumps(result))
except Exception as error:
 print(json.dumps({"exception_type":type(error).__name__,"locations":[{"file":Path(f.filename).name,"line":f.lineno,"function":f.name} for f in traceback.extract_tb(error.__traceback__)[-8:]]}));raise SystemExit(1)
'''.replace("NAMESPACE","/proc/"+str(os.getpid())+"/ns/net")
        write("scripts/host_worker.py",worker_source)
        selected=fixture(); selected.update(binary=str(root/"valkey-server"),binary_sha256=BINARY_SHA,private_dir=str(root/"inputs")); selected["settings"].update(maxmemory_mib=64)
        if image is not None: selected["image"]=image["reference"]
        for i,m in enumerate(selected["members"]):
            m["name"]="cachehost-"+token+"-"+str(i)
            for kind in activation.KINDS:
                path=root/(m["name"]+"-"+kind); path.mkdir(); path.chmod(0o700); os.chown(path,service_uid,service_gid); m[kind+"_dir"]=str(path)
                unit=service.unit_path({"member":m["name"]},kind)
                if unit.exists() or unit.is_symlink(): raise ValueError("A fixture unit already exists")
                if engine is None: units[unit]=None
            run(["/usr/sbin/ip","address","add",m["address"]+"/32","dev","lo"])
        selected["primary"]=selected["members"][0]["name"]; renderer.validate(selected)
        run(["/usr/bin/openssl","req","-x509","-newkey","ec","-pkeyopt","ec_paramgen_curve:P-256","-nodes","-days","1","-addext","basicConstraints=critical,CA:TRUE","-addext","keyUsage=critical,keyCertSign,cRLSign","-subj","/CN=fixture-valkey-host-ca","-keyout",root/"ca.key","-out",root/"ca.crt"]); (root/"ca.key").chmod(0o600)
        files=renderer.render(selected,runtime)
        for name,value in files.items(): write("bundle/"+name,value)
        for m in selected["members"]:
            name=m["name"]; base=root/"inputs"/name; tls=base/"tls"; tls.mkdir(parents=True)
            extension=write(name+"-extension.cnf","subjectAltName=IP:"+m["address"]+"\nextendedKeyUsage=serverAuth,clientAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\n")
            run(["/usr/bin/openssl","req","-new","-newkey","ec","-pkeyopt","ec_paramgen_curve:P-256","-nodes","-subj","/CN="+name,"-keyout",tls/"tls.key","-out",root/(name+".csr")])
            run(["/usr/bin/openssl","x509","-req","-in",root/(name+".csr"),"-CA",root/"ca.crt","-CAkey",root/"ca.key","-CAcreateserial","-days","1","-extfile",extension,"-out",tls/"tls.crt"])
            shutil.copyfile(root/"ca.crt",tls/"ca.crt"); (tls/"tls.key").chmod(0o440); os.chown(tls/"tls.key",0,service_gid)
            for n,v in auth.files(selected,runtime,name,references(),environment=env).items():
                path=write("inputs/"+name+"/auth/"+n,v,0o440); os.chown(path,0,service_gid)
            if engine is None: write("inputs/"+name+"/binary.sha256",files[name+"-binary.sha256"])
            plan=host_plan(hashlib.sha256(files["valkey-contract.json"].encode()).hexdigest(),runtime)
            plan.update(member=name,machine_id_sha256=hashlib.sha256(Path("/etc/machine-id").read_text().strip().encode()).hexdigest(),boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                service_uid=service_uid,service_gid=service_gid,credentials={role+"_env":references()[role] for role in ("probe","discovery")},files_sha256={n:hashlib.sha256((base/n).read_bytes()).hexdigest() for n in activation.FILES},
                storage={kind:{"device":Path(m[kind+"_dir"]).stat().st_dev,"inode":Path(m[kind+"_dir"]).stat().st_ino} for kind in activation.KINDS},
                native={"user":account.pw_name,"group":group.gr_name,"systemctl_sha256":activation.mechanics.file_hash(Path("/usr/bin/systemctl")),"max_runtime_seconds":240} if engine is None else None,
                docker=None if engine is None else {"binary":engine["backend"].binary,"socket":engine["backend"].socket,"binary_sha256":activation.mechanics.file_hash(Path(engine["backend"].binary)),"daemon_id":engine["engine_id"],"server_version":"29.9.3","image_id":image["id"]})
            plans[name]=plan; write(name+"-plan.json",json.dumps(plan),0o600)
        (root/"private").mkdir(mode=0o700)
        for m in selected["members"]:
            name=m["name"]; stage="preflight"; test.assertTrue(worker(name,"check-host")["host_checked"])
            unexpected=Path(m["server_dir"])/"unexpected"; unexpected.write_text("fixture"); worker(name,"check-host",failure=True); unexpected.unlink()
            stage="activation"; test.assertTrue(worker(name,"activate")["tls_authenticated_processes_verified"])
            if engine is None:
                for kind in activation.KINDS:
                    unit=service.unit_path(plans[name],kind); meta=unit.lstat(); units[unit]=(meta.st_dev,meta.st_ino,hashlib.sha256(unit.read_bytes()).hexdigest())
        stage="runtime-verification"; first=selected["members"][0]; tls=root/"inputs"/first["name"]/"tls"; context=ssl.create_default_context(cafile=str(root/"ca.crt")); context.load_cert_chain(tls/"tls.crt",tls/"tls.key")
        trust={}
        for kind,path in (("ca",root/"ca.crt"),("client_cert",tls/"tls.crt"),("client_key",tls/"tls.key")):
            trust[kind+"_file"]=str(path); trust[kind+"_sha256"]=hashlib.sha256(path.read_bytes()).hexdigest()
        pins={}
        for m in selected["members"]:
            with RESP(m["address"],6380,context,"probe",env[references()["probe"]]) as client: server=verifier.info(client.command("INFO","server"))
            with RESP(m["address"],26380,context,"discovery",env[references()["discovery"]]) as client: sentinel=client.command("SENTINEL","MYID")
            cert=(root/"inputs"/m["name"]/"tls/tls.crt").read_text(); pins[m["name"]]={"connect_address":m["address"],"addresses":[m["address"]],"server_run_id":server["run_id"],"sentinel_id":sentinel,"server_cert_sha256":hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert)).hexdigest()}
        runtime={"schema_version":1,"contract_sha256":hashlib.sha256(files["valkey-contract.json"].encode()).hexdigest(),"trust":trust,"credentials":plans[first["name"]]["credentials"],"members":pins,
            "limits":{"request_seconds":5,"operation_seconds":60,"max_response_bytes":524288,"canary_ttl_seconds":180,"replication_wait_ms":3000}}
        with mock.patch.dict(os.environ,env):
            deadline=time.monotonic()+45; transport=verifier.Network(runtime,json.loads(files["valkey-contract.json"]))
            while True:
                try: verifier.snapshot(transport,runtime,json.loads(files["valkey-contract.json"])); break
                except ValueError:
                    if time.monotonic()>=deadline: raise
                    time.sleep(0.5)
            test.assertTrue(verifier.operate(root/"bundle",runtime,live=True,canary=True)["canary_replication_verified"])
        stage="original-resume"
        for name in plans: test.assertTrue(worker(name,"resume")["read_only"])
        stage="changed-invocation-refusal"; changed=selected["members"][1]["name"]
        if engine is None:
            original=service.properties(plans[changed],"server")["InvocationID"]
            run(["/usr/bin/systemctl","restart",activation.name(plans[changed],"server")+".service"]); worker(changed,"resume",failure=True)
            test.assertNotEqual(original,service.properties(plans[changed],"server")["InvocationID"])
        else:
            journal=json.loads((root/"private"/changed/"valkey-host-proof.json").read_text()); handle=journal["resources"]["server"]["handle"]
            original=json.loads(engine["backend"].docker("container","inspect",handle))[0]["State"]["StartedAt"]
            engine["backend"].docker("container","restart",handle); worker(changed,"resume",failure=True)
            test.assertNotEqual(original,json.loads(engine["backend"].docker("container","inspect",handle))[0]["State"]["StartedAt"])
        for filename,digest in commitment.items():
            if hashlib.sha256((source_dir/filename).read_bytes()).hexdigest()!=digest: raise ValueError("Sources changed during actual host acceptance")
        report.update(native_pairs_activated=engine is None,docker_pairs_activated=engine is not None,original_resume_verified=True,runtime_canary_verified=True,changed_invocation_refused=True,nonempty_storage_refused=True,
            namespace_scoped_fixture=True,binary_sha256=BINARY_SHA,source_archive_sha256=SOURCE_SHA,toolset_sha256=activation.tools(),source_sha256={n:commitment[n] for n in ("activate_valkey_host.py","test_valkey_host.py","test_valkey_host_fixture.py")},
            application_compatibility_verified=False,physical_node_loss_verified=False,ha_verified=False,full_stack_verified=False)
        if image is not None: report.update(fixture_image_manifest_sha256=image["reference"].split("@sha256:")[1],fixture_image_id=image["id"],runtime_libraries_sha256=image["libraries_sha256"],publisher_image_verified=False)
    except BaseException:
        print(json.dumps({"valkey_host_fixture_stage":stage,"private_details_suppressed":True}),flush=True); raise
    finally:
        if engine is not None:
            for name,plan in plans.items():
                receipt=root/"private"/name/"valkey-host-proof.json"
                if not receipt.exists(): continue
                journal=json.loads(receipt.read_text())
                for kind in activation.KINDS:
                    expected_name=activation.name(plan,kind); ids=engine["backend"].docker("container","ls","--all","--no-trunc","--filter","name=^/"+expected_name+"$","--format",'{{.ID}}').split()
                    if not ids: continue
                    if len(ids)!=1 or not activation.mechanics.container_id(ids[0]): raise ValueError("Ambiguous fixture container cleanup")
                    row=json.loads(engine["backend"].docker("container","inspect",ids[0]))[0]; original=journal["resources"][kind]["handle"]
                    if row.get("Name")!="/"+expected_name or row.get("Config",{}).get("Labels",{}).get(activation.LABEL+"operation")!=journal["operation_id"] or original is not None and original!=ids[0]: raise ValueError("An unowned container appeared")
                    engine["backend"].docker("container","stop","--time=10",ids[0]); engine["backend"].docker("container","rm",ids[0])
        for unit,identity in units.items():
            if not unit.exists(): continue
            meta=unit.lstat(); content=unit.read_bytes()
            if identity is not None:
                if (meta.st_dev,meta.st_ino,hashlib.sha256(content).hexdigest())!=identity: raise ValueError("An original fixture unit changed before cleanup")
            elif b"# valkey-operation=" not in content or str(root/"valkey-server").encode() not in content: raise ValueError("An unowned unit appeared")
            run(["/usr/bin/systemctl","stop",unit.name],failure=True)
            props=run(["/usr/bin/systemctl","show",unit.name,"--property=MainPID"]).stdout
            if props.strip()!="MainPID=0": raise ValueError("An original fixture process is still active")
            unit.unlink()
        run(["/usr/bin/systemctl","daemon-reload"])
        for unit in units: run(["/usr/bin/systemctl","reset-failed",unit.name],failure=True)
        if root.is_symlink() or root.parent!=parent or not root.name.startswith("platform-valkey-host-") or (root.stat().st_dev,root.stat().st_ino)!=inode: raise ValueError("Unsafe fixture cleanup scope")
        shutil.rmtree(root)
    report["temporary_services_and_data_removed"]=True
    if engine is None and os.environ.get("VALKEY_TEST_REPORT"):
        target=Path(os.environ["VALKEY_TEST_REPORT"])
        with target.open("x") as stream: os.chmod(target,0o600); json.dump(report,stream,sort_keys=True,indent=2); stream.write("\n")
    return report
