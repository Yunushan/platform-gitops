#!/usr/bin/env python3
"""Test dedicated Argo CD cache preparation without changing a running application."""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

import argocd_valkey as integration
import materialize_valkey_auth as auth
import render_argocd_host as host
import render_valkey_ha as renderer
from test_valkey_ha import fixture as cache_fixture
import test_valkey_runtime as protocol_fixture
import test_argocd_identity_api_fixture as api_fixture


def fixture(runtime="raw"):
    cache=cache_fixture(); cache["network"]["client_namespaces"]=["argocd"]; files=renderer.render(cache,runtime); contract=json.loads(files["valkey-contract.json"])
    plan={"schema_version":1,"cache_contract_sha256":hashlib.sha256(files["valkey-contract.json"].encode()).hexdigest(),"application":{"kind":"argocd","version":"3.5.0","namespace":"argocd","private_dir":"/run/platform-argocd-cache","credential_secret":"argocd-cache-credentials","tls_secret":"argocd-cache-tls"}}
    refs={role:"ARGOCD_CACHE_TEST_"+role.upper() for role in integration.ROLES}; values={v:"fixture_only_"+role+"_"*40 for role,v in refs.items()}
    return files,contract,plan,refs,values


class ClientTests(unittest.TestCase):
    def test_compose_binds_both_api_clients_to_the_pinned_sentinel_group(self):
        files, contract, client, _, _ = fixture()
        client["application"]["private_dir"] = host.INPUT + "/cache"
        original = json.loads((host.ROOT / "examples/argocd-host-docker-ui.json").read_text())
        direct = yaml.safe_load(host.render(original)["compose.yaml"])["services"]
        plan = {key: value for key, value in original.items() if key not in {"cache_host", "cache_port"}}

        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            for name, contents in files.items():
                (bundle / name).write_bytes(contents.encode())
            options = {"cache_bundle": bundle, "cache_client_plan": client}
            services = yaml.safe_load(host.render(plan, **options)["compose.yaml"])["services"]
            expected = integration.arguments(contract, client)
            for name in (host.REPO, host.CONTROLLER, host.SERVER):
                service = services[name]
                for flag in expected:
                    self.assertIn(flag, service["command"])
                self.assertFalse(any(arg.startswith("--redis=") for arg in service["command"]))
                for filename in integration.CLIENT_FILES:
                    self.assertIn(f"{host.INPUT}/cache/credentials/{filename}", service["entrypoint"][2])
            for flag in expected:
                self.assertIn("--repo-server-" + flag[2:], services[host.SERVER]["command"])
            for name in (host.APPSET, host.NOTIFICATIONS):
                self.assertEqual(services[name], direct[name])
            with self.assertRaises(ValueError):
                host.render(original, **options)
            with self.assertRaises(ValueError):
                host.render(plan, cache_bundle=bundle)
            (bundle / "cache-a-server.conf").write_text("changed")
            with self.assertRaises(ValueError):
                host.render(plan, **options)

    def test_all_five_runtime_fragments_select_verified_tls_and_three_sentinels(self):
        for runtime in renderer.RUNTIMES:
            files,contract,plan,refs,values=fixture(runtime); rendered=integration.render(contract,plan); args=json.loads(rendered["argocd-valkey-arguments.json"])
            self.assertEqual(len([a for a in args if a.startswith("--sentinel=")]),3); self.assertIn("--redis-use-tls=true",args); self.assertIn("--redis-insecure-skip-tls-verify=false",args)
            self.assertFalse(any(a.startswith("--redis=") for a in args)); self.assertTrue(all(":26380" in a for a in args if a.startswith("--sentinel=")))
            status=json.loads(rendered["argocd-valkey-status.json"])
            for key in integration.FALSE_FLAGS: self.assertFalse(status[key])
            pod=yaml.safe_load(rendered["argocd-valkey-pod-fragment.yaml"])
            self.assertEqual(len(pod["containers"]),3); self.assertFalse(any(v.get("secret",{}).get("data") for v in pod["volumes"]))
            for container in pod["containers"]: self.assertEqual(container["args"],args); self.assertTrue(all(m["readOnly"] for m in container["volumeMounts"]))
            for value in values.values(): self.assertNotIn(value,json.dumps(rendered))
    def test_only_reviewed_version_dedicated_namespace_and_disjoint_mounts_are_accepted(self):
        files,contract,original,refs,values=fixture()
        mutations=[lambda p:p["application"].update(version="latest"),lambda p:p["application"].update(kind="harbor"),lambda p:p["application"].update(private_dir="/run/../etc/cache"),
            lambda p:p["application"].update(private_dir=contract["plan"]["private_dir"]),lambda p:p["application"].update(credential_secret=p["application"]["tls_secret"]),lambda p:p.update(password="<fixture-only>")]
        for change in mutations:
            plan=copy.deepcopy(original); change(plan)
            with self.assertRaises(ValueError): integration.validate(plan,contract)
        shared=copy.deepcopy(contract); shared["plan"]["network"]["client_namespaces"].append("harbor")
        with self.assertRaisesRegex(ValueError,"exclusive"): integration.validate(original,shared)
    def test_original_control_files_are_preserved_and_clients_have_no_admin_grants(self):
        files,contract,plan,refs,values=fixture()
        base=auth.files(contract["plan"],"raw","cache-a",{k:refs[k] for k in auth.ROLES},environment=values)
        result=integration.private_files(contract,plan,"cache-a",refs,environment=values)
        for name in ("server-auth.conf","sentinel-auth.conf"): self.assertEqual(result[name],base[name])
        self.assertTrue(result["server.acl"].startswith(base["server.acl"])); self.assertTrue(result["sentinel.acl"].startswith(base["sentinel.acl"]))
        data=next(v for v in result["server.acl"].splitlines() if v.startswith("user argocd_cache ")); sentinel=next(v for v in result["sentinel.acl"].splitlines() if v.startswith("user argocd_discovery "))
        for command in ("+@all","+config","+acl","+flushall","+flushdb","+eval","+replicaof"): self.assertNotIn(command,data)
        self.assertNotIn("+sentinel|failover",sentinel); self.assertIn("&+switch-master",sentinel); self.assertIn("&+replica-reconf-done",sentinel); self.assertNotIn("allchannels",sentinel)
        self.assertEqual(result["argocd-auth_username"],"argocd_cache\n"); self.assertEqual(result["argocd-sentinel_username"],"argocd_discovery\n")
        for member in contract["plan"]["members"]:
            alternate=integration.private_files(contract,plan,member["name"],refs,environment=values)
            self.assertEqual(alternate["argocd-auth"],result["argocd-auth"])
    def test_role_aliases_missing_values_and_shared_passwords_refuse(self):
        files,contract,plan,original,values=fixture()
        for role in integration.ROLES:
            refs=copy.deepcopy(original); refs[role]=refs["probe"] if role!="probe" else "invalid name"
            with self.assertRaises(ValueError): integration.private_files(contract,plan,"cache-a",refs,environment=values)
            missing={**values,original[role]:""}
            with self.assertRaises(ValueError): integration.private_files(contract,plan,"cache-a",original,environment=missing)
        shared={**values,original["argocd_data"]:values[original["replication"]]}
        with self.assertRaises(ValueError): integration.private_files(contract,plan,"cache-a",original,environment=shared)
    def test_private_create_only_cli_does_not_print_credentials_or_modify_originals(self):
        files,contract,plan,refs,values=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); bundle=root/"bundle"; bundle.mkdir(); (root/"private").mkdir()
            for name,value in files.items(): (bundle/name).write_text(value,encoding="utf-8",newline="\n")
            (root/"plan.json").write_text(json.dumps(plan)); (root/"refs.json").write_text(json.dumps(refs)); output=root/"private/material"; text=io.StringIO()
            args=["--bundle-dir",str(bundle),"--application-plan",str(root/"plan.json"),"--output-dir",str(output),"--materialize","--member","cache-a","--credential-refs",str(root/"refs.json")]
            with mock.patch.object(integration,"ROOT",root),mock.patch.dict(os.environ,values),contextlib.redirect_stdout(text),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(integration.main(args),0); before={p.name:p.read_bytes() for p in output.iterdir()}; self.assertEqual(integration.main(args),1)
                self.assertEqual(before,{p.name:p.read_bytes() for p in output.iterdir()})
            for value in values.values(): self.assertNotIn(value,text.getvalue())
            if os.name=="posix": self.assertTrue(all(p.stat().st_mode&0o777==0o600 for p in output.iterdir()))
            (bundle/"cache-a-server.conf").write_text("changed")
            with self.assertRaises(ValueError): integration.selected(bundle,plan)
    def test_binary_cache_values_keep_exact_bytes_and_the_response_bound(self):
        wire=protocol_fixture.Socket([b"$4\r\n\xff\x00\x81\x80\r\n"]); harness=protocol_fixture.RuntimeTests()
        with harness.connect(wire,bulk_bytes=True) as client: self.assertEqual(client.command("GET","fixture"),b"\xff\x00\x81\x80")
        wire=protocol_fixture.Socket([b"$9999999\r\n"])
        with harness.connect(wire,bulk_bytes=True) as client,self.assertRaises(ValueError): client.command("GET","fixture")
    def test_crd_initially_missing_conditions_are_observed_before_condition_wait(self):
        created={"metadata":{"name":"applications.argoproj.io","uid":"fixture-original"}}
        client=mock.Mock(); client.get.side_effect=[created,{**created,"status":{"conditions":[{"type":"NamesAccepted","status":"True"}]}}]
        with mock.patch.object(api_fixture.time,"sleep") as sleep: api_fixture.await_crd_conditions(client,created)
        self.assertEqual(client.get.call_count,2); sleep.assert_called_once()
        client.get.side_effect=None; client.get.return_value={"metadata":{"uid":"fixture-replacement"}}
        with self.assertRaisesRegex(ValueError,"replaced"): api_fixture.await_crd_conditions(client,created)
    @unittest.skipUnless(os.environ.get("ARGOCD_VALKEY_TEST_RUNTIME")=="1","Explicit disposable real Argo CD/Sentinel/TLS compatibility")
    def test_real_argocd_server_cache_survives_primary_loss(self):
        from test_argocd_valkey_fixture import exercise
        result=exercise(self)
        for key in ("argocd_server_revocation_cache_verified","argocd_sentinel_tls_client_verified","argocd_cache_state_preserved_after_promotion","argocd_cache_reconnected_without_server_restart","argocd_cache_admin_reads_denied","argocd_fresh_server_revocation_read_verified","argocd_post_promotion_revocation_verified","temporary_processes_and_data_removed"): self.assertTrue(result[key])
        for key in ("argocd_server_cache_compatibility_verified","argocd_controller_cache_verified","argocd_repo_server_cache_verified","application_compatibility_verified","physical_node_loss_verified","ha_verified","full_stack_verified"): self.assertFalse(result[key])


if __name__=="__main__": unittest.main()
