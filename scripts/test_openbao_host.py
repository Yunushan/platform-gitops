#!/usr/bin/env python3
"""Check original host/process custody and uncertain-write recovery."""

import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import activate_openbao_host as activate
import render_openbao_ha as renderer
from test_keycloak_host import Host as FakeHost
from test_openbao_ha import fixture


def host_plan(digest, runtime):
    return {"schema_version": 1, "contract_sha256": digest, "member": "bao-a", "machine_id_sha256": "c" * 64, "boot_id": "00000000-0000-0000-0000-000000000010",
        "service_uid": 1000, "service_gid": 1000, "files_sha256": {k: "d" * 64 for k in activate.FILES},
        "storage": {"data": {"device": 1, "inode": 2}, "audit": {"device": 1, "inode": 3}},
        "docker": {"binary": "/usr/bin/docker", "socket": "/var/run/docker.sock", "binary_sha256": "e" * 64, "daemon_id": "fixture-engine", "server_version": "29.9.3", "image_id": "sha256:" + "f" * 64} if runtime == "docker" else None,
        "native": {"user": "openbao", "group": "openbao", "systemctl_sha256": "e" * 64, "max_runtime_seconds": None} if runtime == "raw" else None}


def container(contract, plan, state):
    return {"Id": "1" * 64, "Name": "/" + activate.name(plan), "Image": plan["docker"]["image_id"],
        "Config": {"Image": contract["plan"]["image"], "User": "1000:1000", "Entrypoint": ["/usr/bin/bao"], "Cmd": ["server", "-config=/etc/openbao/config.json"],
            "Env": ["PATH=/usr/bin:/bin"], "Labels": activate.labels(state), "WorkingDir": "/", "Hostname": plan["member"], "StopTimeout": 60, "Tty": False, "OpenStdin": False, "Healthcheck": {"Test": ["NONE"]}},
        "HostConfig": {"ReadonlyRootfs": True, "Privileged": False, "OomKillDisable": None, "NetworkMode": "host", "IpcMode": "none", "CgroupnsMode": "private", "UsernsMode": "host", "Runtime": "runc",
            "PidsLimit": 512, "Memory": 2 * 1024 ** 3, "MemorySwap": 2 * 1024 ** 3, "NanoCpus": 2000000000, "AutoRemove": False,
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}, "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}},
            "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"], "Tmpfs": {"/tmp": "rw,nosuid,nodev,noexec,mode=1777,size=128m"},
            "Mounts": [{"Type": "bind", "Source": s, "Target": t, "ReadOnly": ro, "BindOptions": {"Propagation": "rprivate"}} for s,t,ro in activate.mounts(contract,plan)]},
        "Mounts": [{"Type": "bind", "Source": s, "Destination": t, "RW": not ro, "Propagation": "rprivate"} for s,t,ro in activate.mounts(contract,plan)]}


class HostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup); self.root = Path(temporary.name)
        patch = mock.patch.object(activate, "ROOT", self.root); patch.start(); self.addCleanup(patch.stop); self.counter = 0
    def setup(self, runtime):
        self.counter += 1; self.bundle = self.root / ("bundle-" + str(self.counter)); self.bundle.mkdir()
        files = renderer.render(fixture(), runtime)
        for n,v in files.items(): (self.bundle / n).write_text(v, encoding="utf-8", newline="\n")
        self.contract = json.loads(files["openbao-contract.json"])
        self.plan = host_plan(hashlib.sha256(files["openbao-contract.json"].encode()).hexdigest(), runtime); self.host = FakeHost(); self.operation = self.root / "private" / ("op-" + str(self.counter))
    def action(self, mode): return activate.operate(self.bundle, self.plan, mode=mode, host=self.host, output=self.operation, wait_seconds=5)
    def test_local_mode_has_no_host_or_private_file_reads(self):
        for runtime in ("raw", "docker"):
            self.setup(runtime)
            with mock.patch.object(activate, "Host", side_effect=AssertionError("Host access")): self.assertTrue(activate.operate(self.bundle, self.plan)["local_inputs_verified"])
            self.assertFalse(self.host.calls)
    def test_check_is_read_only_and_leaves_ceremony_external(self):
        for runtime in ("raw", "docker"):
            self.setup(runtime); result = self.action("check-host")
            self.assertTrue(result["read_only"]); self.assertTrue(result["requires_external_ceremony"]); self.assertFalse(self.operation.exists())
            for flag in activate.UNVERIFIED: self.assertIs(result[flag], False)
    def test_activation_resume_retains_original_invocation_without_repeating_writes(self):
        for runtime in ("raw", "docker"):
            self.setup(runtime); result = self.action("activate")
            self.assertTrue(result["original_member_active"]); self.assertTrue(result["tls_serving_uninitialized_verified"])
            before = [x for x in self.host.calls if x in {"create", "reload", "start"}]
            self.assertEqual(before, ["create", "reload", "start"] if runtime == "raw" else ["create", "start"])
            self.assertTrue(self.action("resume")["read_only"])
            self.assertEqual(before, [x for x in self.host.calls if x in {"create", "reload", "start"}])
            for flag in activate.UNVERIFIED: self.assertIs(result[flag], False)
    def test_lost_create_reload_start_acknowledgements_are_only_observed(self):
        for runtime in ("raw", "docker"):
            for stage in (("create", "reload", "start") if runtime == "raw" else ("create", "start")):
                self.setup(runtime); self.host.lost = stage
                with self.assertRaises(TimeoutError): self.action("activate")
                self.assertTrue(self.action("resume")["tls_serving_uninitialized_verified"]); self.assertEqual(self.host.calls.count(stage), 1)
    def test_absent_uncertain_writes_are_never_reissued(self):
        for runtime in ("raw", "docker"):
            for stage in (("create", "reload", "start") if runtime == "raw" else ("create", "start")):
                self.setup(runtime); self.host.unknown = stage
                with self.assertRaises(TimeoutError): self.action("activate")
                before = self.host.calls.count(stage)
                with mock.patch.object(activate.time, "monotonic", side_effect=itertools.count(0,10)), mock.patch.object(activate.time, "sleep"):
                    with self.assertRaises((TimeoutError,ValueError)): self.action("resume")
                self.assertEqual(self.host.calls.count(stage), before)
    def test_existing_replaced_restarted_and_changed_host_refuse(self):
        self.setup("raw"); self.host.original = {"handle": "a" * 64}
        with self.assertRaises(ValueError): self.action("activate")
        for field in ("handle", "policy_sha256", "invocation"):
            self.setup("docker"); self.action("activate"); self.host.original[field] = "9" * 64
            with self.subTest(field=field), self.assertRaises(ValueError): self.action("resume")
        self.setup("raw"); self.action("activate"); self.host.facts["replacement"] = True
        with self.assertRaises(ValueError): self.action("resume")
    def test_changed_package_host_plan_and_credential_literals_refuse(self):
        self.setup("raw"); path = self.bundle / "bao-a.json"; path.write_text("changed")
        with self.assertRaises(ValueError): self.action("local")
        mutations = [lambda p: p.update(service_uid=0), lambda p: p.update(boot_id="wrong"), lambda p: p.update(member="other"),
            lambda p: p["storage"].update(audit=p["storage"]["data"]), lambda p: p["native"].update(user="root"),
            lambda p: p["native"].update(max_runtime_seconds=True), lambda p: p.update(token="<fixture-only>")]
        for i,change in enumerate(mutations):
            self.setup("raw"); change(self.plan)
            with self.subTest(case=i), self.assertRaises(ValueError): self.action("local")
    def test_container_policy_rejects_changed_mounts_privilege_command_and_environment(self):
        self.setup("docker"); state = {"operation_id": "00000000-0000-0000-0000-000000000001", "plan_sha256": "a" * 64, "toolset_sha256": "b" * 64, "handle": None}
        row = container(self.contract, self.plan, state); image = {"Config": {"Env": ["PATH=/usr/bin:/bin"]}}
        digest = activate.container_policy(row,self.contract,self.plan,state,image)
        row["HostConfig"]["OomKillDisable"] = False; self.assertEqual(digest,activate.container_policy(row,self.contract,self.plan,state,image))
        mutations = [lambda r: r["HostConfig"].update(Privileged=True), lambda r: r["HostConfig"].update(MemorySwap=-1), lambda r: r["Config"].update(Cmd=["server","-dev"]),
            lambda r: r["Config"]["Env"].append("VAULT_DEV_ROOT_TOKEN_ID=fixture"), lambda r: r["Mounts"][0].update(RW=True),
            lambda r: r["Mounts"][2].update(Source="/unreviewed"), lambda r: r["HostConfig"].update(CapAdd=["IPC_LOCK"])]
        for i,change in enumerate(mutations):
            candidate = copy.deepcopy(row); change(candidate)
            with self.subTest(case=i), self.assertRaises(ValueError): activate.container_policy(candidate,self.contract,self.plan,state,image)
    def test_health_accepts_only_original_uninitialized_tls_endpoint(self):
        self.setup("raw"); backend = activate.Host(); connection = mock.Mock(); response = mock.Mock(status=501); connection.getresponse.return_value = response
        def value(initialized=False,sealed=True,version="2.7.2"): return json.dumps({"initialized":initialized,"sealed":sealed,"version":version}).encode()
        with mock.patch.object(activate.mechanics,"owns_listener",return_value=True), mock.patch.object(activate,"read_bounded_bytes",return_value=b"fixture-ca"), mock.patch.object(activate.ssl,"create_default_context"), mock.patch.object(activate.http.client,"HTTPSConnection",return_value=connection):
            response.read.side_effect = [value(), b""]; self.assertTrue(backend.healthy(self.contract,self.plan,{}))
            for status, body in ((200,value(True,False)),(503,value(True,True)),(501,value(version="old")),(307,value())):
                response.status=status; response.read.side_effect=[body, b""]; self.assertFalse(backend.healthy(self.contract,self.plan,{}))
        for call in connection.request.call_args_list: self.assertEqual(call.args[:2],("GET","/v1/sys/health"))
    @unittest.skipUnless(os.environ.get("OPENBAO_TEST_HOST") == "1", "Explicit disposable root Linux OpenBao host acceptance")
    def test_real_native_host_activation(self):
        from test_openbao_host_fixture import exercise
        result = exercise(self)
        for flag in ("native_activation_verified", "original_resume_verified", "kernel_listener_owned_verified", "changed_invocation_refused", "temporary_service_and_data_removed"):
            self.assertIs(result[flag],True)
        for flag in ("initialized", "unsealed", "ha_verified", "full_stack_verified"): self.assertIs(result[flag],False)
    @unittest.skipUnless(os.environ.get("OPENBAO_TEST_DOCKER_HOST") == "1", "Explicit disposable Docker OpenBao host acceptance")
    def test_real_docker_host_activation(self):
        from test_openbao_docker_host_fixture import exercise
        result = exercise(self)
        for flag in ("docker_activation_verified", "original_resume_verified", "kernel_listener_owned_verified", "container_cleanup_verified", "engine_cleanup_verified"):
            self.assertIs(result[flag],True)
        for flag in ("publisher_image_verified", "initialized", "unsealed", "ha_verified", "full_stack_verified"): self.assertIs(result[flag],False)


if __name__ == "__main__": unittest.main()
