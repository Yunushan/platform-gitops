#!/usr/bin/env python3
"""Check TLS/Sentinel packages without claiming application compatibility or HA."""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

import materialize_valkey_auth as auth
import render_valkey_ha as renderer


def fixture(): return json.loads((Path(__file__).resolve().parents[1]/"examples/valkey-ha.json").read_text())
def references(): return {role:"VALKEY_FIXTURE_"+role.upper() for role in auth.ROLES}
def credentials(): return {name:"fixture_only_"+role+"_"*40 for role,name in references().items()}


class ValkeyTests(unittest.TestCase):
    def test_all_five_runtimes_pin_files_without_claiming_deployment_or_compatibility(self):
        for runtime in renderer.RUNTIMES:
            files=renderer.render(fixture(),runtime); contract=json.loads(files["valkey-contract.json"]); status=json.loads(files["render-status.json"])
            self.assertEqual(set(contract["files_sha256"]),set(files)-{"valkey-contract.json","render-status.json"})
            for n,h in contract["files_sha256"].items(): self.assertEqual(hashlib.sha256(files[n].encode()).hexdigest(),h)
            for k in ("deployed","tls_verified","replication_verified","failover_verified","application_compatibility_verified","lossless_failover_verified","physical_node_loss_verified","ha_verified","full_stack_verified"): self.assertIs(status[k],False)
    def test_server_tls_persistence_and_write_guard_are_explicit(self):
        plan=fixture()
        for runtime in renderer.RUNTIMES:
            for member in plan["members"]:
                config=renderer.configuration(plan,member,runtime,"server")
                for line in ("port 0\n","tls-port 6380\n","tls-auth-clients yes\n","tls-replication yes\n","appendonly yes\n","appendfsync everysec\n","min-replicas-to-write 1\n","maxmemory-policy noeviction\n","replica-read-only yes\n","repl-ping-replica-period 1\n"):
                    self.assertIn(line,config)
                self.assertEqual("replicaof " in config,member["name"]!=plan["primary"])
                self.assertNotIn("requirepass",config); self.assertNotIn("masterauth",config)
                sentinel=renderer.configuration(plan,member,runtime,"sentinel")
                self.assertIn(" 6380 2\n",sentinel); self.assertIn("tls-port 26380\n",sentinel)
                self.assertLess(sentinel.index("sentinel monitor"),sentinel.index("include "))
    def test_kubernetes_preserves_mutable_roles_and_requires_original_storage_and_secrets(self):
        objects=list(yaml.safe_load_all(renderer.render(fixture(),"k8s")["valkey.yaml"]))
        deployments=[o for o in objects if o["kind"]=="Deployment"]; self.assertEqual(len(deployments),3)
        claims=set()
        for d in deployments:
            self.assertEqual(d["spec"]["strategy"]["type"],"Recreate"); pod=d["spec"]["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"]); self.assertEqual(len(pod["containers"]),2)
            claims.update(v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"] if "persistentVolumeClaim" in v)
            self.assertTrue(all(c["securityContext"]["readOnlyRootFilesystem"] for c in pod["containers"]))
            self.assertTrue(all(c["volumeMounts"][0]["subPath"]=="runtime" for c in pod["containers"]))
            self.assertTrue(all(c["command"][:4]==renderer.ENTRYPOINT for c in pod["containers"]))
            names=[p["name"] for c in pod["containers"] for p in c["ports"]]; self.assertEqual(len(names),len(set(names)))
            self.assertIn(".platform-seed-sha256",pod["initContainers"][0]["command"][2])
        self.assertEqual(len(claims),6); self.assertFalse(any(o["kind"] in {"Secret","PersistentVolumeClaim","Namespace"} for o in objects))
        self.assertEqual(next(o for o in objects if o["kind"]=="PodDisruptionBudget")["spec"]["minAvailable"],2)
        policy=next(o for o in objects if o["kind"]=="NetworkPolicy")["spec"]; self.assertEqual(policy["policyTypes"],["Ingress","Egress"]); self.assertNotIn("0.0.0.0/0",json.dumps(policy))
    def test_native_and_docker_have_bounded_identity_and_separate_writable_config(self):
        plan=fixture()
        for runtime in ("raw","docker"):
            files=renderer.render(plan,runtime)
            for member in plan["members"]:
                for kind in ("server","sentinel"):
                    if runtime=="raw":
                        unit=files[member["name"]+"-"+kind+".service"]
                        for value in ("User=valkey","ProtectSystem=strict","CapabilityBoundingSet=","MemorySwapMax=0","UMask=0077"): self.assertIn(value,unit)
                        self.assertIn("ReadWritePaths="+member[kind+"_dir"],unit)
                    else:
                        service=next(iter(yaml.safe_load(files[member["name"]+"-"+kind+"-compose.yaml"])["services"].values()))
                        self.assertEqual(service["network_mode"],"host"); self.assertEqual(service["user"],"1000:1000")
                        self.assertEqual(service["mem_limit"],service["memswap_limit"]); self.assertTrue(service["read_only"])
                        self.assertEqual(service["entrypoint"],[renderer.ENTRYPOINT[0]]); self.assertEqual(service["command"][:3],renderer.ENTRYPOINT[1:])
                        self.assertTrue(all(v["bind"]["create_host_path"] is False for v in service["volumes"]))
    def test_invalid_topology_credentials_versions_and_unbounded_settings_refuse(self):
        mutations=[lambda p:p.update(version="latest"),lambda p:p.update(image="valkey:latest"),lambda p:p.update(token="<fixture-only>"),
            lambda p:p["members"][1].update(address=p["members"][0]["address"]),lambda p:p["members"][1].update(failure_domain="zone-a"),
            lambda p:p["members"][1].update(sentinel_claim="cache-a-data"),lambda p:p["settings"].update(min_replicas=0),
            lambda p:p["settings"].update(down_after_ms=True),lambda p:p["members"][0].update(server_dir="/etc/platform-valkey-ha/state")]
        for i,change in enumerate(mutations):
            plan=fixture(); change(plan)
            with self.subTest(case=i),self.assertRaises((ValueError,TypeError)): renderer.render(plan,"raw")
    def test_private_auth_uses_distinct_identities_and_no_unscoped_data_client(self):
        plan=fixture(); env=credentials(); result=auth.files(plan,"raw","cache-a",references(),environment=env)
        self.assertEqual(set(result),{"server.acl","sentinel.acl","server-auth.conf","sentinel-auth.conf"})
        self.assertIn("user default reset off",result["server.acl"]); self.assertIn("~platform-probe:*",result["server.acl"])
        self.assertNotIn("+@all",result["server.acl"]); self.assertIn("+@all",result["sentinel.acl"])
        for role in auth.ROLES:
            self.assertNotIn(env[references()[role]],result["server.acl"]+result["sentinel.acl"])
        for runtime in renderer.RUNTIMES: self.assertEqual(len(auth.files(plan,runtime,"cache-a",references(),environment=env)),4)
        for role in auth.ROLES:
            missing=copy.deepcopy(env); missing[references()[role]]=""
            with self.subTest(role=role),self.assertRaises(ValueError): auth.files(plan,"raw","cache-a",references(),environment=missing)
    def test_auth_materialization_is_private_create_only_and_does_not_print_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/"private").mkdir(); plan=root/"plan.json"; plan.write_text(json.dumps(fixture())); refs=root/"refs.json"; refs.write_text(json.dumps(references())); output=root/"private/auth"
            out=io.StringIO()
            with mock.patch.object(auth,"ROOT",root),mock.patch.dict(os.environ,credentials()),contextlib.redirect_stdout(out),contextlib.redirect_stderr(io.StringIO()):
                args=["--plan",str(plan),"--runtime","raw","--member","cache-a","--credential-refs",str(refs),"--output-dir",str(output)]
                self.assertEqual(auth.main(args),0); before={p.name:p.read_bytes() for p in output.iterdir()}; self.assertEqual(auth.main(args),1)
                self.assertEqual(before,{p.name:p.read_bytes() for p in output.iterdir()})
            for value in credentials().values(): self.assertNotIn(value,out.getvalue())
            if os.name=="posix": self.assertTrue(all(p.stat().st_mode&0o777==0o600 for p in output.iterdir()))
    @unittest.skipUnless(os.name=="posix","Actual shell seed checks require POSIX")
    def test_seed_preserves_rewritten_roles_and_refuses_partial_or_changed_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); seed=root/"seed"; seed.mkdir(); volume=root/"volumes"; volume.mkdir()
            for kind in ("server","sentinel"): (volume/kind).mkdir(); (seed/(kind+".conf")).write_text("fixture "+kind+"\n")
            hashes=[hashlib.sha256((seed/(k+".conf")).read_bytes()).hexdigest() for k in ("server","sentinel")]
            script=renderer.seed_script(*hashes,volume_root=str(volume),seed_root=str(seed))
            def run(operation="1"*32): return subprocess.run(["/bin/sh","-c",script],env={**os.environ,"VALKEY_INSTALL_OPERATION_ID":operation},capture_output=True,timeout=10).returncode
            self.assertEqual(run(),0); live=volume/"server/runtime/server.conf"; live.write_text("replicaof rewritten-primary 6380\n"); self.assertEqual(run(),0); self.assertIn("rewritten-primary",live.read_text())
            self.assertNotEqual(run("2"*32),0); self.assertIn("rewritten-primary",live.read_text())
            (volume/"sentinel/runtime/.platform-seed-sha256").unlink(); self.assertNotEqual(run(),0)
            (seed/"server.conf").write_text("changed"); self.assertNotEqual(run(),0); self.assertIn("rewritten-primary",live.read_text())
    @unittest.skipUnless(os.environ.get("VALKEY_TEST_API")=="1","Explicit disposable Kubernetes API schema/admission fixture")
    def test_real_kubernetes_api_admission(self):
        from test_rook_api_fixture import exercise
        exercise(self,resource_objects=list(yaml.safe_load_all(renderer.render(fixture(),"k8s")["valkey.yaml"])))

    @unittest.skipUnless(os.environ.get("VALKEY_TEST_RUNTIME")=="1","Explicit isolated real TLS/Sentinel acceptance")
    def test_real_tls_replication_and_sentinel_failover(self):
        from test_valkey_runtime_fixture import exercise
        result=exercise(self)
        for key in ("tls_replication_verified","restricted_data_scope_verified","sentinel_discovery_verified","primary_process_loss_failover_verified","sentinel_quorum_loss_refused","recovered_quorum_failover_verified","temporary_processes_and_data_removed","runtime_verifier_read_only_verified","runtime_verifier_canary_verified","runtime_verifier_rejoined_topology_verified","runtime_verifier_changed_invocation_refused"):
            self.assertIs(result[key],True)
        for key in ("application_compatibility_verified","lossless_failover_verified","physical_node_loss_verified","ha_verified","full_stack_verified"): self.assertIs(result[key],False)


if __name__=="__main__": unittest.main()
