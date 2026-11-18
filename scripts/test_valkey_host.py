#!/usr/bin/env python3
"""Check pair activation custody and recovery without borrowing running services."""

import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import activate_valkey_host as activation
import render_valkey_ha as renderer
from test_valkey_ha import fixture


def host_plan(digest,runtime):
    return {"schema_version":1,"contract_sha256":digest,"member":"cache-a","machine_id_sha256":"c"*64,"boot_id":"00000000-0000-0000-0000-000000000010",
        "service_uid":1000,"service_gid":1000,"files_sha256":{k:"d"*64 for k in activation.FILES},"credentials":{"probe_env":"VALKEY_HOST_PROBE","discovery_env":"VALKEY_HOST_DISCOVERY"},
        "storage":{"server":{"device":1,"inode":2},"sentinel":{"device":1,"inode":3}},
        "native":{"user":"valkey","group":"valkey","systemctl_sha256":"e"*64,"max_runtime_seconds":None} if runtime=="raw" else None,
        "docker":{"binary":"/usr/bin/docker","socket":"/var/run/docker.sock","binary_sha256":"e"*64,"daemon_id":"fixture-engine","server_version":"29.9.3","image_id":"sha256:"+"f"*64} if runtime=="docker" else None}


class Host:
    def __init__(self): self.calls=[]; self.resources={}; self.seeds={}; self.facts={"host":"fixture"}; self.lost=None; self.unknown=None; self.current=None
    def prerequisites(self,contract,plan,*,starting=False): return copy.deepcopy(self.facts)
    def existing(self,contract,plan,kind): return self.resources.get(kind)
    def empty(self,contract,plan,kind):
        if kind in self.seeds: raise ValueError("Nonempty")
    def before(self,action,kind):
        self.calls.append((action,kind)); self.current=kind
        if self.unknown==(action,kind): raise TimeoutError("Unknown operation")
    def after(self,action,kind):
        if self.lost==(action,kind): self.lost=None; raise TimeoutError("Lost acknowledgement")
    def seed(self,contract,plan,state,kind):
        self.before("seed",kind); self.seeds[kind]="1"*64 if kind=="server" else "2"*64; self.after("seed",kind)
    def inspect_seed(self,contract,plan,state,kind):
        if kind not in self.seeds: raise ValueError("Original seed absent")
        return self.seeds[kind]
    def create(self,contract,plan,state,kind):
        self.before("create",kind); self.resources[kind]={"handle":("a" if kind=="server" else "b")*64,"policy_sha256":"c"*64,"active":False,"loaded":contract["runtime"]=="docker","invocation":None}; self.after("create",kind)
    def inspect(self,contract,plan,state,facts,kind):
        if kind not in self.resources: raise ValueError("Original service absent")
        return copy.deepcopy(self.resources[kind])
    def reload(self):
        kind=self.current; self.before("reload",kind)
        for row in self.resources.values(): row["loaded"]=True
        self.after("reload",kind)
    def start(self,contract,plan,state,kind):
        self.before("start",kind); self.resources[kind].update(active=True,invocation="1"*32); self.after("start",kind)
    def healthy(self,contract,plan,observed,kind): return "d"*64


def container(contract,plan,state,kind):
    return {"Id":"1"*64,"Name":"/"+activation.name(plan,kind),"Image":plan["docker"]["image_id"],
        "Config":{"Image":contract["plan"]["image"],"User":"1000:1000","Entrypoint":[renderer.ENTRYPOINT[0]],"Cmd":activation.command(kind),"Env":["PATH=/usr/bin:/bin"],
            "Labels":activation.labels(state),"WorkingDir":"/","Hostname":plan["member"],"StopTimeout":60,"Tty":False,"OpenStdin":False,"Healthcheck":{"Test":["NONE"]}},
        "HostConfig":{"ReadonlyRootfs":True,"Privileged":False,"OomKillDisable":None,"NetworkMode":"host","IpcMode":"none","CgroupnsMode":"private","UsernsMode":"host","Runtime":"runc","PidsLimit":256,
            "Memory":activation.memory(contract,kind),"MemorySwap":activation.memory(contract,kind),"NanoCpus":2000000000,"AutoRemove":False,"RestartPolicy":{"Name":"unless-stopped","MaximumRetryCount":0},
            "LogConfig":{"Type":"json-file","Config":{"max-size":"10m","max-file":"3"}},"CapDrop":["ALL"],"SecurityOpt":["no-new-privileges:true"],"Tmpfs":{"/tmp":"rw,nosuid,nodev,noexec,mode=1777,size=64m"},
            "Mounts":[{"Type":"bind","Source":s,"Target":t,"ReadOnly":ro,"BindOptions":{"Propagation":"rprivate"}} for s,t,ro in activation.mounts(contract,plan,kind)]},
        "Mounts":[{"Type":"bind","Source":s,"Destination":t,"RW":not ro,"Propagation":"rprivate"} for s,t,ro in activation.mounts(contract,plan,kind)]}


class HostTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.root=Path(tmp.name); patch=mock.patch.object(activation,"ROOT",self.root); patch.start(); self.addCleanup(patch.stop); self.counter=0
    def setup(self,runtime):
        self.counter+=1; self.bundle=self.root/("bundle-"+str(self.counter)); self.bundle.mkdir(); files=renderer.render(fixture(),runtime)
        for name,value in files.items(): (self.bundle/name).write_text(value,encoding="utf-8",newline="\n")
        self.contract=json.loads(files["valkey-contract.json"]); self.plan=host_plan(hashlib.sha256(files["valkey-contract.json"].encode()).hexdigest(),runtime); self.host=Host(); self.output=self.root/"private"/("op-"+str(self.counter))
    def action(self,mode): return activation.operate(self.bundle,self.plan,mode=mode,output=self.output,host=self.host,wait_seconds=5)
    def test_local_and_preflight_never_write_or_certify_the_group(self):
        for runtime in ("raw","docker"):
            self.setup(runtime)
            with mock.patch.object(activation,"Host",side_effect=AssertionError("Host access")): self.assertTrue(self.action("local")["local_inputs_verified"])
            result=self.action("check-host"); self.assertTrue(result["read_only"]); self.assertFalse(self.host.calls); self.assertFalse(self.output.exists())
            for flag in activation.UNVERIFIED: self.assertFalse(result[flag])
    def test_pair_activation_and_readonly_resume_retain_both_originals(self):
        for runtime in ("raw","docker"):
            self.setup(runtime); result=self.action("activate"); self.assertTrue(result["tls_authenticated_processes_verified"]); self.assertTrue(result["original_pair_active"])
            expected=[(action,kind) for kind in activation.KINDS for action in (("seed","create","reload","start") if runtime=="raw" else ("seed","create","start"))]
            self.assertEqual(self.host.calls,expected); self.assertTrue(self.action("resume")["read_only"]); self.assertEqual(self.host.calls,expected)
            for flag in activation.UNVERIFIED: self.assertFalse(result[flag])
    def test_lost_acknowledgements_resume_by_observation_without_repeating_writes(self):
        for runtime in ("raw","docker"):
            for kind in activation.KINDS:
                for action in (("seed","create","reload","start") if runtime=="raw" else ("seed","create","start")):
                    self.setup(runtime); self.host.lost=(action,kind)
                    with self.subTest(runtime=runtime,kind=kind,action=action),self.assertRaises(TimeoutError): self.action("activate")
                    self.assertTrue(self.action("resume")["original_pair_active"]); self.assertEqual(self.host.calls.count((action,kind)),1)
    def test_absent_uncertain_writes_are_not_reissued(self):
        for runtime in ("raw","docker"):
            for action in (("seed","create","reload","start") if runtime=="raw" else ("seed","create","start")):
                self.setup(runtime); self.host.unknown=(action,"server")
                with self.assertRaises(TimeoutError): self.action("activate")
                before=list(self.host.calls)
                with mock.patch.object(activation.time,"monotonic",side_effect=itertools.count(0,10)),mock.patch.object(activation.time,"sleep"):
                    with self.assertRaises((ValueError,TimeoutError)): self.action("resume")
                self.assertEqual(self.host.calls,before)
    def test_existing_replaced_restarted_or_reseeded_services_refuse(self):
        for runtime in ("raw","docker"):
            self.setup(runtime); self.host.resources["sentinel"]={"handle":"a"*64}
            with self.assertRaises(ValueError): self.action("activate")
            for kind in activation.KINDS:
                for field in ("handle","policy_sha256","invocation"):
                    self.setup(runtime); self.action("activate"); self.host.resources[kind][field]="9"*64
                    with self.subTest(kind=kind,field=field),self.assertRaises(ValueError): self.action("resume")
                self.setup(runtime); self.action("activate"); self.host.seeds[kind]="8"*64
                with self.assertRaises(ValueError): self.action("resume")
    def test_package_plan_host_and_private_literals_are_bound(self):
        mutations=[lambda p:p.update(service_uid=0),lambda p:p.update(member="absent"),lambda p:p.update(boot_id="wrong"),lambda p:p.update(password="<fixture-only>"),
            lambda p:p["storage"].update(sentinel=p["storage"]["server"]),lambda p:p["credentials"].update(discovery_env=p["credentials"]["probe_env"]),lambda p:p["native"].update(user="root")]
        for change in mutations:
            self.setup("raw"); change(self.plan)
            with self.assertRaises(ValueError): self.action("local")
        self.setup("raw"); (self.bundle/"cache-a-server.conf").write_text("changed")
        with self.assertRaises(ValueError): self.action("local")
        self.setup("docker"); self.action("activate"); self.host.facts["replacement"]=True
        with self.assertRaises(ValueError): self.action("resume")
    def test_docker_pair_policy_rejects_privilege_environment_and_mount_drift(self):
        self.setup("docker"); state={"operation_id":"00000000-0000-0000-0000-000000000010","plan_sha256":"a"*64,"toolset_sha256":"b"*64,"resources":{k:{"handle":None} for k in activation.KINDS}}; facts={"image":{"Config":{"Env":["PATH=/usr/bin:/bin"]}}}
        for kind in activation.KINDS:
            original=container(self.contract,self.plan,state,kind); self.assertTrue(activation.container_policy(original,self.contract,self.plan,state,facts,kind))
            for change in (lambda r:r["HostConfig"].update(Privileged=True),lambda r:r["HostConfig"].update(MemorySwap=-1),lambda r:r["Config"].update(Cmd=["other"]),
                lambda r:r["Config"].update(Env=["PATH=/foreign"]),lambda r:r["Mounts"][1].update(RW=True),lambda r:r["Config"].update(Labels={})):
                row=copy.deepcopy(original); change(row)
                with self.assertRaises(ValueError): activation.container_policy(row,self.contract,self.plan,state,facts,kind)
            arguments=activation.create_arguments(self.contract,self.plan,state,kind)
            self.assertIn("--pull=never",arguments); self.assertIn("--network=host",arguments); self.assertNotIn("--privileged",arguments)
    @unittest.skipUnless(os.environ.get("VALKEY_TEST_HOST")=="1","Explicit disposable native pair activation")
    def test_real_native_three_pairs_and_original_resume(self):
        from test_valkey_host_fixture import exercise
        result=exercise(self)
        for key in ("native_pairs_activated","original_resume_verified","runtime_canary_verified","changed_invocation_refused","temporary_services_and_data_removed"): self.assertTrue(result[key])
        for key in ("physical_node_loss_verified","application_compatibility_verified","ha_verified","full_stack_verified"): self.assertFalse(result[key])
    @unittest.skipUnless(os.environ.get("VALKEY_TEST_DOCKER_HOST")=="1","Explicit disposable Docker pair activation")
    def test_real_docker_three_pairs_and_original_resume(self):
        from test_valkey_docker_host_fixture import exercise
        result=exercise(self)
        for key in ("docker_pairs_activated","original_resume_verified","runtime_canary_verified","changed_invocation_refused","temporary_services_and_data_removed","engine_cleanup_verified","engine_kernel_isolation_verified"): self.assertTrue(result[key])
        for key in ("publisher_image_verified","physical_node_loss_verified","application_compatibility_verified","ha_verified","full_stack_verified"): self.assertFalse(result[key])


if __name__=="__main__": unittest.main()
