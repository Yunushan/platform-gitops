#!/usr/bin/env python3
"""Test service packaging and guarded exec paths with synthetic private inputs."""

import configparser
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import render_zuul_services as renderer
import start_zuul as launcher
import yaml
from test_zuul_config import environment, fixture, provision


class ZuulServicesTests(unittest.TestCase):
    def test_worker_deployment_callback_requires_one_controller_and_explicit_runtime(self):
        from test_zuul_web_fixture import exercise
        callback = lambda *_args: None
        for selected in (('argocd',), ('flux',)):
            with mock.patch.dict(os.environ, {'ZUUL_RKE2_SERVING_TEST_RUNTIME':'0'}), self.assertRaisesRegex(ValueError, 'explicit isolated'):
                exercise(self, post_controllers=selected, post_deployment=callback)
            with mock.patch.dict(os.environ, {'ZUUL_RKE2_SERVING_TEST_RUNTIME':'1'}), self.assertRaisesRegex(ValueError, 'integrated post-review'):
                exercise(self, post_controllers=selected, post_deployment=callback)
        for selected in ((), ('argocd','flux'), ('jenkins',)):
            with mock.patch.dict(os.environ, {'ZUUL_RKE2_SERVING_TEST_RUNTIME':'1'}), self.assertRaisesRegex(ValueError, 'explicit isolated'):
                exercise(self, post_controllers=selected, post_deployment=callback)

    @unittest.skipUnless(os.environ.get('ZUUL_ZOOKEEPER_TEST_RUNTIME')=='1','explicit pinned Zuul/ZooKeeper runtime required')
    def test_real_zuul_zookeeper_tls_components_and_session_fencing(self):
        from test_zookeeper_runtime_fixture import exercise
        from test_zuul_zookeeper_fixture import coordination
        result=exercise(self,coordination=coordination)
        self.assertTrue(result['actual_zuul_coordination']['real_zuul_component_and_lock_lifecycle_verified'])

    @unittest.skipUnless(os.environ.get('ZUUL_TLS_BUILD_TEST_ARTIFACTS'),'explicit hash-pinned upstream wheel required')
    def test_reproducible_tls_candidate_changes_only_reviewed_client_and_valid_record(self):
        import base64
        import csv
        import hashlib
        import zipfile

        import build_zuul_zookeeper_tls as builder
        source=Path(os.environ['ZUUL_TLS_BUILD_TEST_ARTIFACTS'])/'zuul-14.3.0-py3-none-any.whl'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with mock.patch.object(builder,'ROOT',root):
                first=root/'private/a';second=root/'private/b'
                a=builder.build(source,first);b=builder.build(source,second)
                self.assertEqual(a,b);self.assertEqual(a['candidate_client_sha256'],launcher.ZOOKEEPER_TLS_CLIENTS['14.3.0'])
                with self.assertRaises(ValueError):builder.build(source,first)
            with zipfile.ZipFile(source) as upstream,zipfile.ZipFile(first/source.name) as candidate:
                old=set(upstream.namelist());new=set(candidate.namelist())
                self.assertEqual(new-old,{'zuul-14.3.0.dist-info/PLATFORM_ZK_TLS_CHANGE.txt'});self.assertFalse(old-new)
                changed={name for name in old if upstream.read(name)!=candidate.read(name)}
                self.assertEqual(changed,{builder.TARGET,'zuul-14.3.0.dist-info/RECORD'})
                for name,digest,size in csv.reader(io.StringIO(candidate.read('zuul-14.3.0.dist-info/RECORD').decode())):
                    if not digest:continue
                    data=candidate.read(name);expected='sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')
                    self.assertEqual(digest,expected);self.assertEqual(int(size),len(data))

    def test_zookeeper_tls_paths_cannot_disable_or_replace_provisioned_trust(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'zuul.conf'
            good='[zookeeper]\n'+''.join(key+'='+str(root/'private'/name)+'\n' for key,name in (('tls_cert','zk-client.pem'),('tls_key','zk-client.key'),('tls_ca','zk-ca.pem')))
            config.write_text(good,encoding='utf-8');launcher.verify_zookeeper_tls_config(root)
            for text in ('fixture',good.replace('tls_key=','wrong_key='),good.replace(str(root/'private/zk-ca.pem'),'/unreviewed-ca.pem'),good.replace('tls_key='+str(root/'private/zk-client.key'),'tls_key='),good+'tls_key=duplicate\n'):
                config.write_text(text,encoding='utf-8')
                with self.assertRaises(launcher.ZuulLaunchError):launcher.verify_zookeeper_tls_config(root)

    def test_unreviewed_zookeeper_policy_prevents_exec_and_state_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'config';provision(root);state=Path(directory)/'state';state.mkdir()
            with mock.patch.dict(os.environ,environment(root)),mock.patch.object(launcher.sys,'platform','linux'),mock.patch.object(launcher.os,'geteuid',return_value=1000,create=True),mock.patch.object(launcher.shutil,'which',return_value='/reviewed/zuul-scheduler'),mock.patch.object(launcher,'check_version'),mock.patch.object(launcher,'verify_runtime_zookeeper_tls',side_effect=launcher.ZuulLaunchError('unreviewed client')),mock.patch.object(launcher,'verify_runtime_gerrit_host_pins') as pins,mock.patch.object(launcher.os,'execv') as execute:
                with self.assertRaises(launcher.ZuulLaunchError):launcher.launch('scheduler','14.3.0',config_root=root,state=state)
                execute.assert_not_called();pins.assert_not_called();self.assertFalse((state/'.ssh').exists())
        with self.assertRaises(launcher.ZuulLaunchError):launcher.verify_zookeeper_client_policy('11.0.0')

    @unittest.skipUnless(os.environ.get('ZUUL_GERRIT_SSH_TEST_RUNTIME')=='1','explicit pinned Zuul/Gerrit SSH runtime required')
    def test_real_upstream_gerrit_ssh_host_key_enforcement(self):
        from test_zuul_gerrit_ssh_fixture import exercise
        self.assertTrue(exercise(self)['actual_pinned_zuul_gerrit_ssh_transport_verified'])

    @unittest.skipUnless(os.environ.get('ZUUL_WEB_TEST_RUNTIME')=='1','explicit pinned Zuul web/database runtime required')
    def test_real_guarded_web_pair_with_postgres_and_zookeeper(self):
        from test_zuul_web_fixture import exercise
        self.assertTrue(exercise(self)['actual_guarded_web_pair_verified'])

    @unittest.skipUnless(os.environ.get('ZUUL_INTEGRATED_LAUNCHER_TEST_RUNTIME')=='1','explicit isolated integrated launcher runtime required')
    def test_real_integrated_launcher_provider_configuration(self):
        from test_zuul_web_fixture import exercise
        result=exercise(self,integrated=True)
        self.assertTrue(result['coordination']['actual_web_pair']['actual_integrated_provider_and_launcher_pair_verified'])
        self.assertTrue(result['coordination']['actual_web_pair']['actual_static_nodes_ready_verified'])
        self.assertTrue(result['coordination']['actual_web_pair']['actual_gerrit_change_node_request_and_assignment_verified'])

    @unittest.skipUnless(os.environ.get('ZUUL_EXECUTOR_STARTUP_TEST_RUNTIME')=='1','explicit isolated executor startup runtime required')
    def test_real_guarded_executor_starts_with_supported_ansible(self):
        from test_zuul_web_fixture import exercise
        result=exercise(self,executor_startup=True)
        self.assertTrue(result['coordination']['actual_web_pair']['actual_guarded_executor_startup_verified'])

    @unittest.skipUnless(os.environ.get('ZUUL_REAL_JOB_TEST_RUNTIME')=='1','explicit isolated end-to-end job runtime required')
    def test_real_gerrit_job_on_isolated_worker(self):
        from test_zuul_web_fixture import exercise
        result=exercise(self,integrated=True,executor_startup=True,real_job=True)
        self.assertTrue(result['coordination']['actual_web_pair']['actual_gerrit_job_executed_verified'])
        self.assertTrue(result['coordination']['actual_web_pair']['actual_worker_marker_and_nonroot_identity_verified'])

    @unittest.skipUnless(os.environ.get('ZUUL_POST_HANDOFF_TEST_RUNTIME')=='1','explicit isolated post-review job runtime required')
    def test_real_gerrit_post_buildset_for_gitops_handoff(self):
        from test_zuul_web_fixture import exercise
        result=exercise(self,integrated=True,executor_startup=True,real_job=True,post_handoff=True)
        self.assertTrue(result['coordination']['actual_web_pair']['actual_post_buildset_handoff_evidence_verified'])

    @unittest.skipUnless(os.environ.get('ZUUL_GITOPS_HANDOFF_TEST_RUNTIME')=='1','explicit isolated real GitOps promotion runtime required')
    def test_real_zuul_gerrit_to_gitops_controllers(self):
        from test_zuul_web_fixture import exercise
        result=exercise(self,integrated=True,executor_startup=True,real_job=True,post_handoff=True,post_controllers=('argocd','flux'))
        report=result['coordination']['actual_web_pair']
        for flag in ('actual_post_buildset_handoff_evidence_verified','zuul_argocd_live_promotion_verified',
                     'zuul_argocd_cli_and_replay_verified','actual_argocd_reconciliation_verified',
                     'real_configmap_reconciliation_verified','temporary_native_argocd_scope_removed',
                     'zuul_flux_live_promotion_verified',
                     'zuul_flux_cli_and_replay_verified','actual_flux_reconciliation_verified',
                     'temporary_native_flux_scope_removed'):
            self.assertIs(report[flag],True)

    @unittest.skipUnless(os.environ.get('ZUUL_EXECUTOR_RECOVERY_TEST_RUNTIME')=='1','explicit isolated executor recovery runtime required')
    def test_real_executor_crash_and_second_gerrit_job(self):
        from test_zuul_web_fixture import exercise
        result=exercise(self,integrated=True,executor_startup=True,real_job=True,executor_recovery=True)
        self.assertTrue(result['coordination']['actual_web_pair']['actual_executor_crash_restart_and_second_job_verified'])

    @unittest.skipUnless(os.environ.get('ZUUL_INFLIGHT_RECOVERY_TEST_RUNTIME')=='1','explicit isolated in-flight recovery runtime required')
    def test_real_executor_crash_during_running_job(self):
        from test_zuul_web_fixture import exercise
        result=exercise(self,integrated=True,executor_startup=True,real_job=True,inflight_recovery=True)
        self.assertTrue(result['coordination']['actual_web_pair']['actual_inflight_executor_loss_retry_verified'])

    def test_pin_validation_uses_the_selected_cli_python_without_shell_or_environment_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);interpreter=root/'python3';interpreter.write_bytes(b'fixture')
            binary=root/'zuul-scheduler';binary.write_text('#!'+str(interpreter)+'\n',encoding='utf-8')
            with mock.patch.object(launcher,'run_bounded',return_value=subprocess.CompletedProcess([],0,'','')) as run:
                launcher.verify_runtime_gerrit_host_pins(str(binary),root/'config',root/'known_hosts')
                args=run.call_args.args[0];self.assertEqual(args[0],str(interpreter));self.assertEqual(args[1:4],['-I','-B','-c'])
                self.assertEqual(args[-2:],[str(root/'config'),str(root/'known_hosts')]);self.assertEqual(run.call_args.kwargs['env'],{'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
            for line in ('#!/bin/sh\n','#!/usr/bin/env python3\n','missing interpreter\n'):
                binary.write_text(line,encoding='utf-8')
                with self.assertRaises(launcher.ZuulLaunchError):launcher.verify_runtime_gerrit_host_pins(str(binary),root/'config',root/'known_hosts')
            binary.write_text('#!'+str(interpreter)+'\n',encoding='utf-8')
            with mock.patch.object(launcher,'run_bounded',return_value=subprocess.CompletedProcess([],1,'','private details')),self.assertRaises(launcher.ZuulLaunchError):
                launcher.verify_runtime_gerrit_host_pins(str(binary),root/'config',root/'known_hosts')

    def test_gerrit_http_policy_rejects_string_or_disabled_tls_before_sdk_import(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'zuul.conf'
            for value in ('true','false','True','/unreviewed/ca.pem',''):
                path.write_text('[connection "gerrit"]\ndriver=gerrit\nserver=review.example.com\nverify_ssl='+value+'\n',encoding='utf-8')
                with self.assertRaises(launcher.ZuulLaunchError):launcher.verify_gerrit_http_policy(path)

    def test_gerrit_ssh_fixture_rejects_implicit_or_nonloopback_profiles_before_resources(self):
        from test_gerrit_review_runtime_fixture import server
        with mock.patch('test_gerrit_review_runtime_fixture.namespace') as namespace:
            for value in (1,'true',None):
                with self.assertRaises(ValueError),server(ssh=value):pass
            with self.assertRaises(ValueError),server(ssh=True,worker_context={}):pass
            for value in (1,'true',None):
                with self.assertRaises(ValueError),server(observe_config_requests=value):pass
            with self.assertRaises(ValueError),server(observe_config_requests=True,worker_context={}):pass
            namespace.assert_not_called()

    def test_gerrit_pin_selectors_match_actual_driver_host_and_port(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'zuul.conf'
            for text,expected in (
                ('[connection "gerrit"]\ndriver=gerrit\nserver=review.example.com\n',['[review.example.com]:29418']),
                ('[connection "gerrit"]\ndriver=gerrit\nserver=review.example.com\nssh_server=ssh.example.com\nport=22\n',['ssh.example.com']),
                ('[connection "gerrit"]\ndriver=gerrit\nserver=::1\nport=29418\n',['[::1]:29418'])):
                path.write_text(text,encoding='utf-8');self.assertEqual(launcher.gerrit_ssh_endpoints(path),expected)
            for text in ('[connection "gerrit"]\ndriver=gerrit\nserver=bad host\n','[connection "gerrit"]\ndriver=gerrit\nserver=host\nport=0\n','[connection "gerrit"]\ndriver=gerrit\nserver=host\nport=22\nport=23\n','[connection "other"]\ndriver=git\nserver=host\n'):
                path.write_text(text,encoding='utf-8')
                with self.assertRaises(launcher.ZuulLaunchError):launcher.gerrit_ssh_endpoints(path)

    @unittest.skipUnless(importlib.util.find_spec('paramiko'),'actual Paramiko parser required')
    def test_actual_parser_requires_exact_endpoint_and_supports_hashed_pins(self):
        import paramiko
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'zuul.conf';source=root/'known_hosts'
            config.write_text('[connection "gerrit"]\ndriver=gerrit\nserver=review.example.com\nport=29418\n',encoding='utf-8')
            key=paramiko.RSAKey.generate(2048);endpoint='[review.example.com]:29418'
            line=key.get_name()+' '+key.get_base64()
            for name in (endpoint,paramiko.HostKeys.hash_host(endpoint)):
                source.write_text(name+' '+line+'\n',encoding='utf-8');launcher.verify_gerrit_host_pins(config,source)
            for text in ('','# empty\n','review.example.com '+line+'\n','[review.example.com]:29419 '+line+'\n','*.example.com '+line+'\n',endpoint+' ssh-rsa invalid-base64\n','@revoked '+endpoint+' '+line+'\n'):
                source.write_text(text,encoding='utf-8')
                with self.assertRaises(launcher.ZuulLaunchError):launcher.verify_gerrit_host_pins(config,source)

    def test_missing_endpoint_pin_prevents_exec_and_trust_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'config';provision(root);state=Path(directory)/'state';state.mkdir()
            with mock.patch.dict(os.environ,environment(root)),mock.patch.object(launcher.sys,'platform','linux'),mock.patch.object(launcher.os,'geteuid',return_value=1000,create=True),mock.patch.object(launcher.shutil,'which',return_value='/reviewed/zuul-scheduler'),mock.patch.object(launcher,'check_version'),mock.patch.object(launcher,'verify_runtime_zookeeper_tls'),mock.patch.object(launcher,'verify_runtime_gerrit_host_pins',side_effect=launcher.ZuulLaunchError('missing pin')),mock.patch.object(launcher.os,'execv') as execute:
                with self.assertRaises(launcher.ZuulLaunchError):launcher.launch('scheduler','14.3.0',config_root=root,state=state)
                execute.assert_not_called();self.assertFalse((state/'.ssh').exists())

    def test_kubernetes_core_is_replicated_and_no_executor_is_faked(self):
        for runtime in ("rke2", "k3s", "k8s"):
            objects = list(yaml.safe_load_all(renderer.render(fixture(), runtime)["zuul-services.yaml"]))
            deployments = [o for o in objects if o["kind"] == "Deployment"]
            self.assertEqual({o["metadata"]["name"] for o in deployments}, {"zuul-scheduler", "zuul-merger", "zuul-web"})
            for deployment in deployments:
                self.assertEqual(deployment["spec"]["replicas"], 2)
                pod = deployment["spec"]["template"]["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertTrue(pod["securityContext"]["runAsNonRoot"])
                self.assertIn("requiredDuringSchedulingIgnoredDuringExecution", pod["affinity"]["podAntiAffinity"])
                container = pod["containers"][0]
                self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
                self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
                self.assertIn("--expected-version", container["command"])
                self.assertEqual(len([e for e in container["env"] if "valueFrom" in e]), 3)
                self.assertFalse(any(v.get("hostPath") for v in pod["volumes"]))
            self.assertEqual(len([o for o in objects if o["kind"] == "PodDisruptionBudget"]), 3)
            self.assertFalse(any(o["kind"] == "Secret" for o in objects))

    def test_native_executor_requires_the_same_guarded_launcher(self):
        files = renderer.render(fixture(), "raw")
        unit = files["zuul-executor.service"]
        self.assertIn("start_zuul.py --role executor --start --expected-version", unit)
        self.assertIn("User=zuul", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertNotIn("docker", unit)
        self.assertIn("StateDirectory=zuul", unit)
        self.assertFalse(json.loads(files["render-status.json"])["ci_verified"])

    def test_version_check_rejects_other_development_and_ambiguous_versions(self):
        for text, valid in (("Zuul version: 11.0.0", True), ("Zuul 12.0.0", False),
                            ("Zuul 11.0.0.dev1", False), ("11.0.0 Python 3.12.0", False)):
            with self.subTest(text=text), mock.patch.object(launcher, "run_bounded", return_value=subprocess.CompletedProcess([], 0, text, "")):
                if valid:
                    launcher.check_version("fixture-binary", "11.0.0")
                else:
                    with self.assertRaises(launcher.ZuulLaunchError):
                        launcher.check_version("fixture-binary", "11.0.0")

    def test_known_host_pins_are_staged_but_never_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "provided"
            source.write_bytes(b"approved-public-host-key-fixture")
            state = root / "state"
            state.mkdir()
            launcher.stage_known_hosts(source, state)
            launcher.stage_known_hosts(source, state)
            pin = state / ".ssh/known_hosts"
            before = pin.read_bytes()
            source.write_bytes(b"different-public-host-key-fixture")
            with self.assertRaises(launcher.ZuulLaunchError):
                launcher.stage_known_hosts(source, state)
            self.assertEqual(pin.read_bytes(), before)

    def test_core_launch_checks_then_uses_fixed_exec_vector(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            provision(root)
            state = Path(directory) / "state"
            state.mkdir()
            with mock.patch.dict(os.environ, environment(root)), mock.patch.object(launcher.sys, "platform", "linux"), \
                    mock.patch.object(launcher.os, "geteuid", return_value=1000, create=True), mock.patch.object(launcher.shutil, "which", return_value="/reviewed/zuul-scheduler"), \
                    mock.patch.object(launcher, "verify_runtime_zookeeper_tls") as tls, mock.patch.object(launcher, "check_version") as check, mock.patch.object(launcher, "verify_runtime_gerrit_host_pins") as pins, mock.patch.object(launcher.os, "execv") as execute:
                launcher.launch("scheduler", "11.0.0", config_root=root, state=state)
                check.assert_called_once_with("/reviewed/zuul-scheduler", "11.0.0")
                tls.assert_called_once_with("/reviewed/zuul-scheduler", root, "11.0.0")
                self.assertEqual(pins.call_count,2)
                execute.assert_called_once_with("/reviewed/zuul-scheduler", ["/reviewed/zuul-scheduler", "-f", "-c", str(root / "zuul.conf")])
                self.assertIn("StrictHostKeyChecking=yes", os.environ["GIT_SSH_COMMAND"])
                self.assertEqual(os.environ["HOME"], str(state))
                self.assertEqual(os.environ['GIT_SSL_CAINFO'],str(root/'private/gerrit-ca.pem'))
                self.assertEqual(os.environ['GIT_CONFIG_VALUE_0'],'true')

    def test_launch_preserves_literal_percent_in_upstream_environment_interpolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'config';provision(root);state=Path(directory)/'state';state.mkdir()
            values=environment(root);values['ZUUL_KEYSTORE_PASSWORD']='fixture-%20-%(OTHER)s-%%'
            values['ZUUL_GERRIT_HTTP_PASSWORD']='fixture-%25-%(ZUUL_DATABASE_URI)s'
            with mock.patch.dict(os.environ,values),mock.patch.object(launcher.sys,'platform','linux'),mock.patch.object(launcher.os,'geteuid',return_value=1000,create=True),mock.patch.object(launcher.shutil,'which',return_value='/reviewed/zuul-web'),mock.patch.object(launcher,'check_version'),mock.patch.object(launcher,'verify_runtime_zookeeper_tls'),mock.patch.object(launcher,'verify_runtime_gerrit_host_pins'),mock.patch.object(launcher.os,'execv'):
                launcher.launch('web','14.3.0',config_root=root,state=state)
                parsed=configparser.ConfigParser({k:os.environ[k] for k in values if k.startswith('ZUUL_')})
                parsed.read_string('[fixture]\n'+''.join('value_'+str(i)+'=%('+k+')s\n' for i,k in enumerate(('ZUUL_DATABASE_URI','ZUUL_KEYSTORE_PASSWORD','ZUUL_GERRIT_HTTP_PASSWORD'))))
                for i,key in enumerate(('ZUUL_DATABASE_URI','ZUUL_KEYSTORE_PASSWORD','ZUUL_GERRIT_HTTP_PASSWORD')):self.assertEqual(parsed['fixture']['value_'+str(i)],values[key])

    def test_executor_probe_failure_prevents_exec(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            provision(root)
            state = Path(directory) / "state"
            state.mkdir()
            with mock.patch.dict(os.environ, environment(root)), mock.patch.object(launcher.sys, "platform", "linux"), \
                    mock.patch.object(launcher.os, "geteuid", return_value=1000, create=True), mock.patch.object(launcher.shutil, "which", return_value="/reviewed/zuul-executor"), \
                    mock.patch.object(launcher, "check_version"), mock.patch.object(launcher, "verify_runtime_zookeeper_tls"), mock.patch.object(launcher, "verify_runtime_gerrit_host_pins"), mock.patch.object(launcher, "executor_checks", side_effect=launcher.ZuulLaunchError("probe failed")), \
                    mock.patch.object(launcher.os, "execv") as execute:
                with self.assertRaises(launcher.ZuulLaunchError):
                    launcher.launch("executor", "11.0.0", config_root=root, state=state)
                execute.assert_not_called()
                self.assertFalse((state / ".ssh").exists())

    def test_git_verification_overrides_are_refused_before_inputs_or_state_writes(self):
        for name,value in (('GIT_SSL_NO_VERIFY','true'),('GIT_CONFIG_PARAMETERS',"'http.sslVerify=false'"),('GIT_CONFIG_COUNT','1')):
            with mock.patch.dict(os.environ,{name:value}),mock.patch.object(launcher.sys,'platform','linux'),mock.patch.object(launcher.os,'geteuid',return_value=1000,create=True),mock.patch.object(launcher,'verify_inputs') as inputs:
                with self.assertRaises(launcher.ZuulLaunchError):launcher.launch('merger','14.3.0')
                inputs.assert_not_called()

    def test_executor_has_no_root_or_privilege_fallback(self):
        with mock.patch.object(launcher.sys, "platform", "linux"), mock.patch.object(launcher.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(launcher, "run_bounded") as runner:
            with self.assertRaises(launcher.ZuulLaunchError):
                launcher.executor_checks(Path("/etc/zuul/zuul.conf"), Path("/var/lib/zuul"), "/reviewed/zuul-executor")
            runner.assert_not_called()

    def test_executor_checks_ansible_and_namespace_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            def result(arguments, **kwargs):
                if arguments[0].endswith("bwrap"):
                    self.assertIn("--unshare-all", arguments)
                    self.assertIn("--clearenv", arguments)
                    self.assertIn("--ro-bind", arguments)
                    self.assertNotIn("--bind", arguments)
                    self.assertTrue(any("/probe/created" in value for value in arguments))
                else:
                    self.assertIn("AnsibleManager().validate() is True", arguments[-1])
                    self.assertIn("-I", arguments)
                return subprocess.CompletedProcess(arguments, 0, "", "")
            with mock.patch.object(launcher.sys, "platform", "linux"), mock.patch.object(launcher.os, "geteuid", return_value=1000, create=True), \
                    mock.patch.object(launcher.os, "readlink", return_value="net:[fixture]"), \
                    mock.patch.object(launcher.shutil, "which", side_effect=lambda name: "/reviewed/" + name), \
                    mock.patch.object(launcher, "runtime_python", return_value="/reviewed/python3"), \
                    mock.patch.object(launcher, "run_bounded", side_effect=result) as runner:
                launcher.executor_checks(Path("/etc/zuul/zuul.conf"), state, "/reviewed/zuul-executor")
                self.assertEqual(runner.call_count, 2)
            self.assertEqual(list(state.iterdir()), [])

    def test_failed_ansible_validation_does_not_probe_or_start(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(launcher.sys, "platform", "linux"), mock.patch.object(launcher.os, "geteuid", return_value=1000, create=True), \
                    mock.patch.object(launcher.shutil, "which", side_effect=lambda name: "/reviewed/" + name), \
                    mock.patch.object(launcher, "runtime_python", return_value="/reviewed/python3"), \
                    mock.patch.object(launcher, "run_bounded", return_value=subprocess.CompletedProcess([], 1, "", "")) as runner:
                with self.assertRaises(launcher.ZuulLaunchError):
                    launcher.executor_checks(Path("/etc/zuul/zuul.conf"), Path(directory), "/reviewed/zuul-executor")
                self.assertEqual(runner.call_count, 1)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_executor_refuses_a_different_ansible_interpreter(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(launcher.sys, "platform", "linux"), \
                mock.patch.object(launcher.os, "geteuid", return_value=1000, create=True), \
                mock.patch.object(launcher.shutil, "which", side_effect=lambda name: "/reviewed/" + name), \
                mock.patch.object(launcher, "runtime_python", side_effect=["/env-a/bin/python", "/env-b/bin/python"]), \
                mock.patch.object(launcher, "run_bounded") as runner:
            with self.assertRaisesRegex(launcher.ZuulLaunchError, "same reviewed interpreter"):
                launcher.executor_checks(Path(directory) / "zuul.conf", Path(directory), "/reviewed/zuul-executor")
            runner.assert_not_called()

    def test_immutable_bundle_changes_when_launcher_changes(self):
        plan = fixture()
        files = renderer.bundle(plan)
        first = next(yaml.safe_load_all(renderer.kubernetes(plan, files)))["metadata"]["name"]
        files["start_zuul.py"] += "\n# synthetic-revision\n"
        second = next(yaml.safe_load_all(renderer.kubernetes(plan, files)))["metadata"]["name"]
        self.assertNotEqual(first, second)

    def test_docker_never_exposes_executor_or_public_plaintext_web(self):
        services = yaml.safe_load(renderer.render(fixture(), "docker")["zuul-compose.yaml"])["services"]
        self.assertNotIn("zuul-executor", services)
        self.assertEqual(services["zuul-web"]["ports"], ["127.0.0.1:9000:9000"])
        for service in services.values():
            self.assertEqual(service["command"], [])
            self.assertFalse(service.get("privileged", False))
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertTrue(all(not m["bind"]["create_host_path"] for m in service["volumes"]))

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI unavailable")
    def test_real_compose_parser_with_synthetic_environment(self):
        version = subprocess.run(["docker", "compose", "version", "--short"], capture_output=True, timeout=20, check=False)
        if version.returncode:
            self.skipTest("Compose plugin unavailable")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zuul-compose.yaml"
            path.write_text(renderer.render(fixture(), "docker")["zuul-compose.yaml"], encoding="utf-8")
            env = {**os.environ, **{name: "fixture-only" for name in renderer.SECRET_KEYS}}
            result = subprocess.run(["docker", "compose", "-f", str(path), "config", "--quiet"],
                                    env=env, capture_output=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, "Compose rejected the synthetic core-services manifest")

    def test_cli_does_not_start_any_process_without_explicit_start(self):
        with mock.patch.object(launcher, "verify_inputs", return_value={"read_only": True}), \
                mock.patch.object(launcher, "launch") as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(["--role", "scheduler"]), 0)
            launch.assert_not_called()
        with mock.patch.object(launcher, "launch") as launch, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(launcher.main(["--role", "scheduler", "--start"]), 1)
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
