#!/usr/bin/env python3
"""Actual native host-helper acceptance in a disposable private network namespace."""

import hashlib
import json
import os
from pathlib import Path
import pwd
import grp
import shutil
import sys
import tarfile
import tempfile
from uuid import uuid4

from bounded_subprocess import run_bounded
import activate_openbao_host as activation
import render_openbao_ha as renderer
from test_openbao_ha import fixture
from test_openbao_host import host_plan
from test_openbao_runtime_fixture import ARCHIVE, ARCHIVE_BYTES, ARCHIVE_SHA256


def exercise(test):
    if sys.platform != "linux" or os.geteuid() != 0 or os.environ.get("OPENBAO_TEST_HOST") != "1" or not Path("/run/systemd/system").is_dir(): raise ValueError("Select the explicit disposable root Linux host fixture")
    if Path("/proc/self/ns/net").stat().st_ino == Path("/proc/1/ns/net").stat().st_ino: raise ValueError("Use a fresh isolated network namespace")
    source_dir = Path(__file__).resolve().parent
    commitment = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_dir.glob("*.py")}
    root = Path(tempfile.mkdtemp(prefix="platform-openbao-host-", dir="/run")); root.chmod(0o755); inode = (root.stat().st_dev,root.stat().st_ino)
    identity = "baohost-" + uuid4().hex[:12]; unit = Path("/etc/systemd/system") / ("platform-openbao-" + identity + ".service")
    unit_inode = None; unit_digest = None; stage = "preparation"; report = {}
    account = pwd.getpwnam("nobody"); group = grp.getgrgid(account.pw_gid); service = activation.Host()
    def run(args, *, seconds=60, failure=False):
        result = run_bounded(list(map(str,args)), env=activation.mechanics.CLEAN, input="", text=True, timeout=seconds, output_max_bytes=256 * 1024, check=False)
        if result.returncode and not failure: raise ValueError("An isolated host fixture command failed")
        return result
    def write(name,value,mode=0o644):
        path = root/name; path.parent.mkdir(parents=True,exist_ok=True)
        for parent in [*reversed(path.parent.parents),path.parent]:
            if parent.is_relative_to(root): parent.chmod(0o755)
        path.write_text(value,encoding="utf-8",newline="\n"); path.chmod(mode); return path
    def worker(mode, *, failure=False):
        result=run(["/usr/bin/python3","-B",root/"scripts/host_worker.py",root/"host-plan.json",root/"bundle",mode,root/"private/operation"],seconds=180,failure=True)
        if failure:
            if result.returncode != 1: raise ValueError("A fixture refusal did not occur")
            return None
        if result.returncode:
            try: diagnostic=json.loads(result.stdout)
            except ValueError: diagnostic={"worker_failed":True}
            print(json.dumps({"openbao_host_safe_failure":diagnostic}),flush=True); raise ValueError("Native host activation fixture failed")
        return json.loads(result.stdout)
    try:
        if unit.exists() or unit.is_symlink(): raise ValueError("A fixture unit already exists")
        interfaces=json.loads(run(["/usr/sbin/ip","-j","address","show"]).stdout)
        if len(interfaces)!=1 or interfaces[0]["ifname"]!="lo" or interfaces[0].get("addr_info"): raise ValueError("The fixture namespace is not empty")
        run(["/usr/sbin/ip","link","set","lo","up"]); run(["/usr/sbin/ip","address","add","192.0.2.41/32","dev","lo"])
        artifact=Path(os.environ["OPENBAO_TEST_ARTIFACTS"])/ARCHIVE
        if artifact.is_symlink() or artifact.stat().st_size!=ARCHIVE_BYTES: raise ValueError("Use the bounded reviewed release")
        with artifact.open("rb") as stream:
            if hashlib.file_digest(stream,"sha256").hexdigest()!=ARCHIVE_SHA256: raise ValueError("The release archive differs")
        with tarfile.open(artifact) as archive:
            entries=[m for m in archive.getmembers() if m.name=="bao"]
            if len(entries)!=1 or not entries[0].isfile() or not 0<entries[0].size<256*1024**2: raise ValueError("Invalid release executable")
            with archive.extractfile(entries[0]) as source,(root/"bao").open("xb") as output: shutil.copyfileobj(source,output,length=1024*1024)
        (root/"bao").chmod(0o755); binary_sha=activation.mechanics.file_hash(root/"bao")
        for path in source_dir.glob("*.py"):
            target=root/"scripts"/path.name; target.parent.mkdir(exist_ok=True); shutil.copyfile(path,target); target.chmod(0o644)
        # Only namespace placement is test-specific. All production host checks,
        # unit content, start/inspection and TLS/cgroup logic execute unchanged.
        write("scripts/host_worker.py",'''import json,sys,traceback
from pathlib import Path
import activate_openbao_host as activation
original=activation.unit_text
def scoped(contract,plan,state):
 return original(contract,plan,state)+"\\n[Service]\\nNetworkNamespacePath=NAMESPACE\\n"
activation.unit_text=scoped
try:
 plan=json.loads(Path(sys.argv[1]).read_text())
 result=activation.operate(Path(sys.argv[2]),plan,mode=sys.argv[3],output=Path(sys.argv[4]),wait_seconds=45)
 print(json.dumps(result))
except Exception as error:
 print(json.dumps({"exception_type":type(error).__name__,"locations":[{"file":Path(f.filename).name,"line":f.lineno,"function":f.name} for f in traceback.extract_tb(error.__traceback__)[-8:]]}));raise SystemExit(1)
'''.replace("NAMESPACE","/proc/"+str(os.getpid())+"/ns/net"))
        selected=fixture(); selected.update(binary=str(root/"bao"),binary_sha256=binary_sha,private_dir=str(root/"inputs"),audit_dir=str(root/"audit"))
        selected["members"][0]["name"]=identity
        for m in selected["members"]: m["data_dir"]=str(root/("data-"+m["name"]))
        chosen=selected["members"][0]; private=root/"inputs"/identity; tls=private/"tls"; tls.mkdir(parents=True)
        for path in (root/"inputs",private,tls): path.chmod(0o755)
        for path in (Path(chosen["data_dir"]),root/"audit"):
            path.mkdir(); os.chown(path,account.pw_uid,account.pw_gid); path.chmod(0o700)
        run(["/usr/bin/openssl","req","-x509","-newkey","ec","-pkeyopt","ec_paramgen_curve:P-256","-nodes","-days","1",
            "-addext","basicConstraints=critical,CA:TRUE","-addext","keyUsage=critical,keyCertSign,cRLSign","-subj","/CN=fixture-host-ca","-keyout",root/"ca.key","-out",tls/"ca.crt"])
        (root/"ca.key").chmod(0o600)
        extension=write("extensions.cnf","subjectAltName=IP:192.0.2.41\nextendedKeyUsage=serverAuth,clientAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\n")
        run(["/usr/bin/openssl","req","-new","-newkey","ec","-pkeyopt","ec_paramgen_curve:P-256","-nodes","-subj","/CN="+identity,"-keyout",tls/"tls.key","-out",root/"member.csr"])
        run(["/usr/bin/openssl","x509","-req","-in",root/"member.csr","-CA",tls/"ca.crt","-CAkey",root/"ca.key","-CAcreateserial","-days","1","-extfile",extension,"-out",tls/"tls.crt"])
        serial=tls/"ca.srl"
        if serial.exists(): serial.unlink()
        (tls/"tls.key").chmod(0o440); os.chown(tls/"tls.key",0,account.pw_gid)
        for path in (tls/"ca.crt",tls/"tls.crt"): path.chmod(0o644)
        files=renderer.render(selected,"raw")
        for n,v in files.items(): write("bundle/"+n,v)
        write("inputs/"+identity+"/config.json",files[identity+".json"])
        write("inputs/"+identity+"/binary.sha256",files[identity+"-binary.sha256"])
        plan=host_plan(hashlib.sha256(files["openbao-contract.json"].encode()).hexdigest(),"raw")
        plan.update(member=identity,machine_id_sha256=hashlib.sha256(Path("/etc/machine-id").read_text().strip().encode()).hexdigest(),boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),service_uid=account.pw_uid,service_gid=account.pw_gid,
            files_sha256={n:hashlib.sha256((private/n).read_bytes()).hexdigest() for n in activation.FILES},
            storage={k:{"device":p.stat().st_dev,"inode":p.stat().st_ino} for k,p in (("data",Path(chosen["data_dir"])),("audit",root/"audit"))},
            native={"user":account.pw_name,"group":group.gr_name,"systemctl_sha256":activation.mechanics.file_hash(Path("/usr/bin/systemctl")),"max_runtime_seconds":180})
        write("host-plan.json",json.dumps(plan),0o600); (root/"private").mkdir(mode=0o700)
        stage="preflight"; test.assertTrue(worker("check-host")["host_checked"])
        unexpected=Path(chosen["data_dir"])/"unexpected"; unexpected.write_text("fixture")
        worker("check-host",failure=True); unexpected.unlink()
        stage="activation"; result=worker("activate"); test.assertTrue(result["tls_serving_uninitialized_verified"])
        info=unit.lstat(); unit_inode=(info.st_dev,info.st_ino); unit_digest=hashlib.sha256(unit.read_bytes()).hexdigest()
        journal=json.loads((root/"private/operation/openbao-host-proof.json").read_text()); invocation=journal["invocation"]
        stage="original-resume"; test.assertTrue(worker("resume")["read_only"])
        props=service.properties(plan); test.assertEqual(props["InvocationID"],invocation)
        test.assertTrue(activation.mechanics.owns_listener({"pid":int(props["MainPID"]),"cgroup":props["ControlGroup"]},"192.0.2.41",8200))
        stage="changed-invocation-refusal"; run(["/usr/bin/systemctl","restart",unit.name]); worker("resume",failure=True)
        if service.properties(plan)["InvocationID"]==invocation: raise ValueError("The fixture restart did not change invocation")
        for filename,digest in commitment.items():
            if hashlib.sha256((source_dir/filename).read_bytes()).hexdigest()!=digest: raise ValueError("Fixture sources changed during acceptance")
        report.update(native_activation_verified=True,original_resume_verified=True,kernel_listener_owned_verified=True,changed_invocation_refused=True,
            nonempty_storage_refused=True,version=selected["version"],archive_sha256=ARCHIVE_SHA256,binary_sha256=binary_sha,toolset_sha256=activation.tools(),
            source_sha256={n:commitment[n] for n in ("activate_openbao_host.py","test_openbao_host.py","test_openbao_host_fixture.py")},
            initialized=False,unsealed=False,ha_verified=False,full_stack_verified=False)
    except BaseException:
        print(json.dumps({"openbao_native_fixture_stage":stage,"failure_details_suppressed":True}),flush=True); raise
    finally:
        if unit.exists():
            info=unit.lstat()
            if unit_inode is None:
                content=unit.read_text()
                if "# openbao-operation=" not in content or str(root/"bao") not in content: raise ValueError("An unowned unit appeared")
            elif (info.st_dev,info.st_ino)!=unit_inode or hashlib.sha256(unit.read_bytes()).hexdigest()!=unit_digest: raise ValueError("The original fixture unit changed before cleanup")
            run(["/usr/bin/systemctl","stop",unit.name],failure=True)
            if service.properties({"member":identity}).get("MainPID") not in {"0",""}: raise ValueError("The fixture process is still active")
            unit.unlink(); run(["/usr/bin/systemctl","daemon-reload"]); run(["/usr/bin/systemctl","reset-failed",unit.name],failure=True)
        if root.is_symlink() or (root.stat().st_dev,root.stat().st_ino)!=inode or root.parent!=Path("/run") or not root.name.startswith("platform-openbao-host-"): raise ValueError("Unsafe fixture cleanup scope")
        shutil.rmtree(root)
    report["temporary_service_and_data_removed"]=True
    output=os.environ.get("OPENBAO_TEST_REPORT")
    if output:
        target=Path(output)
        with target.open("x") as stream: os.chmod(target,0o600); json.dump(report,stream,sort_keys=True,indent=2); stream.write("\n")
    return report
