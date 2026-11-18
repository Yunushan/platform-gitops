#!/usr/bin/env python3
"""Actual paired Valkey containers on a private engine and isolated network."""

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from unittest import mock

import activate_valkey_host as activation
from bounded_subprocess import run_bounded
import test_docker_engine_fixture as engines
import test_valkey_host_fixture as hosts


REGISTRY_SHA="40df2224d410f72ae425c3371873b078bbdbda3b8b612be9571f0e6751f3acc8"


def prepare_image(root,engine,binary):
    backend=engine["backend"]
    def docker(*args): return engine["run"]([backend.binary,"--host=unix://"+backend.socket,*args])
    archive=Path(os.environ["VALKEY_TEST_REGISTRY_ARTIFACTS"])/"registry_3.1.2_linux_amd64.tar.gz"
    if archive.is_symlink() or not 0<archive.stat().st_size<128*1024**2: raise ValueError("Select the bounded private registry archive")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream,"sha256").hexdigest()!=REGISTRY_SHA: raise ValueError("The reviewed registry changed")
    with tarfile.open(archive) as tar:
        entries=[m for m in tar.getmembers() if m.name=="registry"]
        if len(entries)!=1 or not entries[0].isfile() or not 0<entries[0].size<128*1024**2: raise ValueError("Invalid registry executable")
        with tar.extractfile(entries[0]) as source,(root/"registry").open("xb") as output: shutil.copyfileobj(source,output,length=1024*1024)
    (root/"registry").chmod(0o755)
    linked=engine["run"](["/usr/bin/ldd",binary])+engine["run"](["/usr/bin/ldd","/bin/sh"]); paths=[]
    for line in linked.splitlines():
        matches=re.findall(r"(/[^\s]+)",line)
        paths.extend(matches)
    paths=sorted(set(paths))
    if not 3<=len(paths)<=16 or any(not p.startswith(("/lib/","/lib64/","/usr/lib/")) for p in paths): raise ValueError("Unexpected dynamic runtime inventory")
    libraries={}
    with tarfile.open(root/"valkey-rootfs.tar","w") as tar:
        inventory=[("usr/local/bin/valkey-server",binary),("bin/sh",Path("/bin/sh").resolve(strict=True)),*[(p.lstrip("/"),Path(p).resolve(strict=True)) for p in paths]]
        for name,path in inventory:
            digest=activation.mechanics.file_hash(path,maximum=64*1024**2)
            if name!="usr/local/bin/valkey-server": libraries[name]=digest
            item=tarfile.TarInfo(name); item.mode=0o555; item.size=path.stat().st_size
            with path.open("rb") as stream: tar.addfile(item,stream)
    with socket.socket() as reservation: reservation.bind(("127.0.0.1",0)); port=reservation.getsockname()[1]
    storage=engine["storage"]/"valkey-registry"; storage.mkdir(mode=0o700)
    config=root/"registry.yml"; config.write_text("version: 0.1\nlog:\n  level: error\nstorage:\n  filesystem:\n    rootdirectory: "+str(storage)+"\nhttp:\n  addr: 127.0.0.1:"+str(port)+"\n  secret: <fixture-only>\n"); config.chmod(0o600)
    registry=None
    with (root/"registry.log").open("xb") as output:
        try:
            registry=subprocess.Popen([str(root/"registry"),"serve",str(config)],env=activation.mechanics.CLEAN,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+20
            while True:
                if registry.poll() is not None: raise ValueError("The private registry exited")
                connection=http.client.HTTPConnection("127.0.0.1",port,timeout=2)
                try:
                    connection.request("GET","/v2/"); response=connection.getresponse(); response.read(4096)
                    if response.status==200: break
                except OSError: pass
                finally: connection.close()
                if time.monotonic()>=deadline: raise TimeoutError("Private registry startup deadline")
                time.sleep(0.2)
            tag="127.0.0.1:"+str(port)+"/fixture/valkey:8.1.11"
            docker("image","import",str(root/"valkey-rootfs.tar"),tag); docker("image","push",tag)
            image=json.loads(docker("image","inspect",tag))[0]; digests=[v for v in image.get("RepoDigests",[]) if v.startswith(tag.rsplit(":",1)[0]+"@sha256:")]
            if len(digests)!=1: raise ValueError("The private image lacks a unique registry digest")
            docker("image","pull",digests[0])
            return {"reference":digests[0],"id":image["Id"],"libraries_sha256":libraries}
        finally:
            if registry is not None and registry.poll() is None: registry.terminate(); registry.wait(timeout=15)


def exercise(test):
    if sys.platform!="linux" or os.geteuid()!=0 or os.environ.get("VALKEY_TEST_DOCKER_HOST")!="1": raise ValueError("Select the explicit disposable Docker host fixture")
    if Path("/proc/self/ns/net").stat().st_ino==Path("/proc/1/ns/net").stat().st_ino: raise ValueError("Select an isolated private namespace")
    source_dir=Path(__file__).resolve().parent; source={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source_dir.glob("*.py")}
    result=run_bounded(["/usr/sbin/ip","-j","address","show"],input="",text=True,env=activation.mechanics.CLEAN,timeout=10,output_max_bytes=128*1024,check=True)
    rows=json.loads(result.stdout)
    if len(rows)!=1 or rows[0]["ifname"]!="lo" or rows[0].get("addr_info"): raise ValueError("The fixture namespace is not fresh")
    run_bounded(["/usr/sbin/ip","link","set","lo","up"],input="",text=True,env=activation.mechanics.CLEAN,timeout=10,output_max_bytes=128*1024,check=True)
    namespace="/proc/"+str(os.getpid())+"/ns/net"; original=engines.run_bounded
    def scoped(args,**kwargs):
        values=list(map(str,args))
        if values and values[0]=="/usr/bin/systemd-run": values.insert(1,"--property=NetworkNamespacePath="+namespace)
        return original(values,**kwargs)
    with mock.patch.object(engines,"run_bounded",side_effect=scoped): report=engines.exercise(test,workload=lambda engine:hosts.exercise(test,engine=engine))
    result=report["workload"]; result["engine_cleanup_verified"]=report["docker_engine_cleanup_verified"]; result["engine_kernel_isolation_verified"]=report["docker_kernel_isolation_verified"]
    for name,digest in source.items():
        if hashlib.sha256((source_dir/name).read_bytes()).hexdigest()!=digest: raise ValueError("Sources changed during Docker pair acceptance")
    result["source_sha256"][Path(__file__).name]=source[Path(__file__).name]
    if os.environ.get("VALKEY_TEST_REPORT"):
        target=Path(os.environ["VALKEY_TEST_REPORT"])
        with target.open("x") as stream: os.chmod(target,0o600); json.dump(result,stream,sort_keys=True,indent=2); stream.write("\n")
    return result
