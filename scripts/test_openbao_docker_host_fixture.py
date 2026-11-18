#!/usr/bin/env python3
"""Real release binary in an explicit disposable image/engine and private netns."""

import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from unittest import mock
from uuid import uuid4

import activate_openbao_host as activation
from bounded_subprocess import run_bounded
import render_openbao_ha as renderer
import test_docker_engine_fixture as engines
from test_openbao_ha import fixture
from test_openbao_host import host_plan
from test_openbao_runtime_fixture import ARCHIVE, ARCHIVE_BYTES, ARCHIVE_SHA256


REGISTRY_SHA = "40df2224d410f72ae425c3371873b078bbdbda3b8b612be9571f0e6751f3acc8"


def exercise(test):
    if sys.platform != "linux" or os.geteuid() != 0 or os.environ.get("OPENBAO_TEST_DOCKER_HOST") != "1": raise ValueError("Select the explicit disposable Docker host fixture")
    if Path("/proc/self/ns/net").stat().st_ino == Path("/proc/1/ns/net").stat().st_ino: raise ValueError("Use an isolated private network namespace")
    sources = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob("*.py")}
    def run(args):
        result=run_bounded(list(map(str,args)),input="",env=activation.mechanics.CLEAN,text=True,timeout=60,output_max_bytes=256*1024,check=False)
        if result.returncode: raise ValueError("Isolated Docker fixture command failed")
        return result.stdout
    rows=json.loads(run(["/usr/sbin/ip","-j","address","show"]))
    if len(rows)!=1 or rows[0]["ifname"]!="lo" or rows[0].get("addr_info"): raise ValueError("The disposable namespace is not fresh")
    run(["/usr/sbin/ip","link","set","lo","up"])
    # Linux installs its normal loopback address when the device comes up.
    # Add only our isolated member address; do not re-add 127.0.0.1.
    run(["/usr/sbin/ip","address","add","192.0.2.41/32","dev","lo"])
    namespace="/proc/"+str(os.getpid())+"/ns/net"
    original_runner=engines.run_bounded
    def namespace_runner(args,**kwargs):
        values=list(map(str,args))
        if values and values[0]=="/usr/bin/systemd-run": values.insert(1,"--property=NetworkNamespacePath="+namespace)
        return original_runner(values,**kwargs)
    with mock.patch.object(engines,"run_bounded",side_effect=namespace_runner): report=engines.exercise(test,workload=lambda engine: workload(test,engine))
    result=report["workload"]; result["engine_cleanup_verified"]=report["docker_engine_cleanup_verified"]
    for name,digest in sources.items():
        if hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()!=digest: raise ValueError("Fixture sources changed during acceptance")
    result["source_sha256"]={n:sources[n] for n in ("activate_openbao_host.py","test_openbao_host.py","test_openbao_docker_host_fixture.py")}
    result["toolset_sha256"]=activation.tools()
    output=os.environ.get("OPENBAO_TEST_REPORT")
    if output:
        target=Path(output)
        with target.open("x") as stream: os.chmod(target,0o600); json.dump(result,stream,sort_keys=True,indent=2); stream.write("\n")
    return result


def workload(test,engine):
    root=engine["root"]; backend=engine["backend"]; run=engine["run"]
    identity="baodocker-"+uuid4().hex[:12]; container_id=None; registry=None; registry_log=None; stage="preparation"
    root.chmod(0o755)
    def docker(*args): return run([backend.binary,"--host=unix://"+backend.socket,*args])
    def write(name,text,mode=0o644):
        path=root/name; path.parent.mkdir(parents=True,exist_ok=True)
        for parent in [*reversed(path.parent.parents),path.parent]:
            if parent.is_relative_to(root): parent.chmod(0o755)
        path.write_text(text,encoding="utf-8",newline="\n"); path.chmod(mode); return path
    def worker(mode):
        result=run_bounded(["/usr/bin/python3","-B",str(root/"scripts/docker_host_worker.py"),str(root/"host-plan.json"),str(root/"bundle"),mode,str(root/"private/operation")],
            env=activation.mechanics.CLEAN,text=True,timeout=180,output_max_bytes=256*1024,check=False)
        if result.returncode:
            try: diagnostic=json.loads(result.stdout)
            except ValueError: diagnostic={"worker_failed":True}
            print(json.dumps({"openbao_docker_safe_failure":diagnostic}),flush=True); raise ValueError("Docker host activation failed")
        return json.loads(result.stdout)
    def owned():
        row=json.loads(docker("container","inspect",container_id))[0]
        if row.get("Id")!=container_id or row.get("Name")!="/platform-openbao-"+identity: raise ValueError("Original fixture container changed")
        return row
    try:
        artifacts=Path(os.environ["OPENBAO_TEST_ARTIFACTS"]); archive=artifacts/ARCHIVE
        if archive.is_symlink() or archive.stat().st_size!=ARCHIVE_BYTES: raise ValueError("Use the reviewed release archive")
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream,"sha256").hexdigest()!=ARCHIVE_SHA256: raise ValueError("Release archive changed")
        with tarfile.open(archive) as tar:
            entries=[m for m in tar.getmembers() if m.name=="bao"]
            if len(entries)!=1 or not entries[0].isfile() or not 0<entries[0].size<256*1024**2: raise ValueError("Invalid bounded binary")
            with tar.extractfile(entries[0]) as source,(root/"bao").open("xb") as output: shutil.copyfileobj(source,output,length=1024*1024)
        (root/"bao").chmod(0o755); binary_sha=activation.mechanics.file_hash(root/"bao")
        registry_archive=Path(os.environ["OPENBAO_TEST_REGISTRY_ARTIFACTS"])/"registry_3.1.2_linux_amd64.tar.gz"
        with registry_archive.open("rb") as stream:
            if registry_archive.is_symlink() or registry_archive.stat().st_size>128*1024**2 or hashlib.file_digest(stream,"sha256").hexdigest()!=REGISTRY_SHA: raise ValueError("The reviewed registry differs")
        with tarfile.open(registry_archive) as tar:
            entries=[m for m in tar.getmembers() if m.name=="registry"]
            if len(entries)!=1 or not entries[0].isfile() or not 0<entries[0].size<128*1024**2: raise ValueError("Invalid registry executable")
            with tar.extractfile(entries[0]) as source,(root/"registry").open("xb") as output: shutil.copyfileobj(source,output,length=1024*1024)
        (root/"registry").chmod(0o755)
        with socket.socket() as reservation: reservation.bind(("127.0.0.1",0)); port=reservation.getsockname()[1]
        storage=engine["storage"]/"openbao-registry"; storage.mkdir(mode=0o700)
        config=write("registry.yml","version: 0.1\nlog:\n  level: error\nstorage:\n  filesystem:\n    rootdirectory: "+str(storage)+"\nhttp:\n  addr: 127.0.0.1:"+str(port)+"\n  secret: <fixture-only>\n")
        registry_log=(root/"registry.log").open("xb")
        registry=subprocess.Popen([str(root/"registry"),"serve",str(config)],env=activation.mechanics.CLEAN,stdout=registry_log,stderr=subprocess.STDOUT,start_new_session=True)
        deadline=time.monotonic()+20
        while True:
            if registry.poll() is not None: raise ValueError("The disposable registry exited")
            connection=http.client.HTTPConnection("127.0.0.1",port,timeout=2)
            try:
                connection.request("GET","/v2/"); response=connection.getresponse(); response.read(4096)
                if response.status==200: break
            except OSError: pass
            finally: connection.close()
            if time.monotonic()>deadline: raise TimeoutError("Disposable registry startup deadline")
            time.sleep(0.2)
        stage="private-image-preparation"
        with tarfile.open(root/"rootfs.tar","w") as tar:
            item=tarfile.TarInfo("usr/bin/bao"); item.mode=0o555; item.size=(root/"bao").stat().st_size
            with (root/"bao").open("rb") as stream: tar.addfile(item,stream)
        tag="127.0.0.1:"+str(port)+"/fixture/openbao:2.7.2"
        docker("image","import",str(root/"rootfs.tar"),tag); docker("image","push",tag)
        image=json.loads(docker("image","inspect",tag))[0]
        digests=[v for v in image.get("RepoDigests",[]) if v.startswith(tag.rsplit(":",1)[0]+"@sha256:")]
        if len(digests)!=1: raise ValueError("The disposable image has no unique registry digest")
        image_ref=digests[0]; docker("image","pull",image_ref)
        for path in Path(__file__).parent.glob("*.py"):
            target=root/"scripts"/path.name; target.parent.mkdir(exist_ok=True); shutil.copyfile(path,target); target.chmod(0o644)
        write("scripts/docker_host_worker.py",'''import json,sys,traceback
from pathlib import Path
import activate_openbao_host as activation
try:
 plan=json.loads(Path(sys.argv[1]).read_text())
 print(json.dumps(activation.operate(Path(sys.argv[2]),plan,mode=sys.argv[3],output=Path(sys.argv[4]),wait_seconds=45)))
except Exception as error:
 print(json.dumps({"exception_type":type(error).__name__,"locations":[{"file":Path(f.filename).name,"line":f.lineno,"function":f.name} for f in traceback.extract_tb(error.__traceback__)[-8:]]}));raise SystemExit(1)
''')
        selected=fixture(); selected.update(image=image_ref,binary=str(root/"bao"),binary_sha256=binary_sha,private_dir=str(root/"inputs"),audit_dir=str(root/"audit")); selected["members"][0]["name"]=identity
        for m in selected["members"]: m["data_dir"]=str(root/("data-"+m["name"]))
        chosen=selected["members"][0]; private=root/"inputs"/identity; tls=private/"tls"; tls.mkdir(parents=True)
        for path in (root/"inputs",private,tls): path.chmod(0o755)
        for path in (Path(chosen["data_dir"]),root/"audit"): path.mkdir(); os.chown(path,1000,1000); path.chmod(0o700)
        run(["/usr/bin/openssl","req","-x509","-newkey","ec","-pkeyopt","ec_paramgen_curve:P-256","-nodes","-days","1","-addext","basicConstraints=critical,CA:TRUE","-addext","keyUsage=critical,keyCertSign,cRLSign","-subj","/CN=fixture-docker-ca","-keyout",root/"ca.key","-out",tls/"ca.crt"])
        (root/"ca.key").chmod(0o600)
        extension=write("extensions.cnf","subjectAltName=IP:192.0.2.41\nextendedKeyUsage=serverAuth,clientAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\n")
        run(["/usr/bin/openssl","req","-new","-newkey","ec","-pkeyopt","ec_paramgen_curve:P-256","-nodes","-subj","/CN="+identity,"-keyout",tls/"tls.key","-out",root/"member.csr"])
        run(["/usr/bin/openssl","x509","-req","-in",root/"member.csr","-CA",tls/"ca.crt","-CAkey",root/"ca.key","-CAcreateserial","-days","1","-extfile",extension,"-out",tls/"tls.crt"])
        if (tls/"ca.srl").exists(): (tls/"ca.srl").unlink()
        (tls/"tls.key").chmod(0o440); os.chown(tls/"tls.key",0,1000)
        for p in (tls/"tls.crt",tls/"ca.crt"): p.chmod(0o644)
        files=renderer.render(selected,"docker")
        for n,v in files.items(): write("bundle/"+n,v)
        write("inputs/"+identity+"/config.json",files[identity+".json"])
        plan=host_plan(hashlib.sha256(files["openbao-contract.json"].encode()).hexdigest(),"docker")
        plan.update(member=identity,machine_id_sha256=hashlib.sha256(Path("/etc/machine-id").read_text().strip().encode()).hexdigest(),boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            files_sha256={n:hashlib.sha256((private/n).read_bytes()).hexdigest() for n in activation.FILES},
            storage={k:{"device":p.stat().st_dev,"inode":p.stat().st_ino} for k,p in (("data",Path(chosen["data_dir"])),("audit",root/"audit"))},
            docker={"binary":backend.binary,"socket":backend.socket,"binary_sha256":activation.mechanics.file_hash(Path(backend.binary)),"daemon_id":engine["engine_id"],"server_version":"29.9.3","image_id":image["Id"]})
        write("host-plan.json",json.dumps(plan),0o600); (root/"private").mkdir(mode=0o700)
        stage="preflight"; test.assertTrue(worker("check-host")["host_checked"])
        stage="activation"; result=worker("activate"); test.assertTrue(result["tls_serving_uninitialized_verified"])
        journal=json.loads((root/"private/operation/openbao-host-proof.json").read_text()); container_id=journal["handle"]; before=owned()["State"]["StartedAt"]
        stage="original-resume"; test.assertTrue(worker("resume")["read_only"]); row=owned(); test.assertEqual(row["State"]["StartedAt"],before)
        original={"pid":row["State"]["Pid"],"cgroup":activation.mechanics.process_cgroup(row["State"]["Pid"])}
        test.assertTrue(activation.mechanics.owns_listener(original,"192.0.2.41",8200))
        report={"docker_activation_verified":True,"original_resume_verified":True,"kernel_listener_owned_verified":True,"archive_sha256":ARCHIVE_SHA256,
            "binary_sha256":binary_sha,"fixture_image_id":image["Id"],"fixture_image_manifest_sha256":image_ref.split("@sha256:")[1],
            "publisher_image_verified":False,"initialized":False,"unsealed":False,"ha_verified":False,"full_stack_verified":False}
    except BaseException:
        print(json.dumps({"openbao_docker_fixture_stage":stage,"details_suppressed":True}),flush=True); raise
    finally:
        if container_id is None and (root/"private/operation/openbao-host-proof.json").exists():
            journal=json.loads((root/"private/operation/openbao-host-proof.json").read_text()); candidate=journal.get("handle")
            if activation.mechanics.container_id(candidate): container_id=candidate
        if container_id:
            owned(); docker("container","stop","--time=10",container_id); owned(); docker("container","rm",container_id)
        if registry is not None and registry.poll() is None: registry.terminate(); registry.wait(timeout=15)
        if registry_log is not None: registry_log.close()
    report["container_cleanup_verified"]=True
    return report
