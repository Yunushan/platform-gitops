#!/usr/bin/env python3
"""Test Zuul configuration and local input checks without contacting a forge."""

import configparser
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode, urlunsplit

import render_zuul_config as renderer
import yaml
import zuul_runtime_preflight as preflight


def fixture():
    return {"schema_version": 1, "zuul_version": "11.0.0",
        "image": "registry.example.com/platform/zuul@sha256:" + "e" * 64,
        "namespace": "zuul", "web_url": "https://ci.example.com/",
        "zookeeper": [{"host": f"zk-{index}.example.com", "port": 2281} for index in range(3)],
        "gerrit": {"host": "review.example.com", "port": 29418, "user": "zuul"},
        "tenants": [{"name": "platform", "config_branch": "main", "config_projects": ["platform/ci-config"],
                     "untrusted_projects": ["applications/example"]}]}


def environment(root: Path):
    query = urlencode({"sslmode": "verify-full", "sslrootcert": str(root / "private/postgres-ca.pem")})
    return {"ZUUL_DATABASE_URI": urlunsplit(("postgresql", "db.example.com", "/zuul", query, "")),
            "ZUUL_KEYSTORE_PASSWORD": "fixture-only", "ZUUL_GERRIT_HTTP_PASSWORD": "fixture-only",
            "ZUUL_EXECUTOR_HOSTNAME": "executor.example.com",
            "REQUESTS_CA_BUNDLE": str(root / "private/gerrit-ca.pem")}


def provision(root: Path):
    root.mkdir(parents=True)
    (root / "private").mkdir()
    for name in ("zuul.conf", "tenants.yaml"):
        (root / name).write_text("fixture", encoding="utf-8")
    for name in (*preflight.PRIVATE_FILES, "build-ssh.key"):
        (root / "private" / name).write_bytes(b"non-secret-test-fixture")


class ZuulConfigurationTests(unittest.TestCase):
    def test_configuration_is_structured_and_secrets_are_references(self):
        config = configparser.RawConfigParser()
        config.read_string(renderer.configuration(fixture()))
        self.assertEqual(config['database']['dburi'], '%(ZUUL_DATABASE_URI)s')
        self.assertEqual(config['keystore']['password'], '%(ZUUL_KEYSTORE_PASSWORD)s')
        self.assertEqual(config['connection "gerrit"']['password'], '%(ZUUL_GERRIT_HTTP_PASSWORD)s')
        self.assertFalse(config.has_option('connection "gerrit"','verify_ssl'))
        self.assertEqual(config['executor']['manage_ansible'], 'false')
        self.assertEqual(len(config['zookeeper']['hosts'].split(',')), 3)
        for key in ('tls_cert', 'tls_key', 'tls_ca'):
            self.assertTrue(config['zookeeper'][key].startswith('/etc/zuul/private/'))

    def test_gerrit_https_port_is_explicit_and_independent_of_ssh(self):
        plan=fixture();default=renderer.configuration(plan)
        plan['gerrit']['https_port']=443;self.assertEqual(renderer.configuration(plan),default)
        plan['gerrit']['https_port']=18443
        parsed=configparser.RawConfigParser();parsed.read_string(renderer.configuration(plan))
        self.assertEqual(parsed['connection "gerrit"']['baseurl'],'https://review.example.com:18443')
        self.assertEqual(parsed['connection "gerrit"']['port'],'29418')
        for value in (True,0,65536,'443',None,'443\nverify_ssl=false'):
            plan['gerrit']['https_port']=value
            with self.assertRaises(renderer.ZuulConfigError):renderer.configuration(plan)

    def test_trust_boundaries_and_explicit_branch_are_preserved(self):
        tenants = yaml.safe_load(renderer.tenant_configuration(fixture()))
        source = tenants[0]['tenant']['source']['gerrit']
        self.assertEqual(source['config-projects'], [{'platform/ci-config': {'load-branch': 'main'}}])
        self.assertEqual(source['untrusted-projects'], ['applications/example'])
        plan = fixture()
        plan['tenants'][0]['untrusted_projects'].append('platform/ci-config')
        with self.assertRaises(renderer.ZuulConfigError):
            renderer.render(plan, 'rke2')

    def test_all_runtime_contracts_remain_explicitly_incomplete(self):
        for runtime in renderer.RUNTIMES:
            with self.subTest(runtime=runtime):
                files = renderer.render(fixture(), runtime)
                status = json.loads(files['render-status.json'])
                self.assertFalse(status['deployed'])
                self.assertFalse(status['ci_verified'])
                self.assertFalse(status['ha_verified'])
                self.assertIn('executor-bubblewrap-and-preinstalled-ansible', status['remaining'])
                self.assertEqual(json.loads(files['secret-contract.json'])['environment'], list(preflight.ENVIRONMENT))
                self.assertEqual(json.loads(files['secret-contract.json'])['private_files'], list(preflight.PRIVATE_FILES))

    def test_invalid_or_injectable_plan_values_rejected(self):
        cases = [None, {}, {**fixture(), 'password': 'not-allowed'}]
        for key, value in (('web_url', 'http://ci.example.com/'), ('web_url', 'https://ci.example.com/subpath'),
                           ('image', 'image:latest'), ('zuul_version', 'latest'), ('namespace', '../escape')):
            plan = fixture()
            plan[key] = value
            cases.append(plan)
        plan = fixture()
        plan['gerrit']['host'] = 'review.example.com\n[arbitrary]'
        cases.append(plan)
        plan = fixture()
        plan['zookeeper'][1] = copy.deepcopy(plan['zookeeper'][0])
        cases.append(plan)
        plan = fixture()
        plan['zookeeper'][0]['port'] = True
        cases.append(plan)
        plan = fixture()
        plan['tenants'][0]['config_branch'] = '../escape'
        cases.append(plan)
        for plan in cases:
            with self.subTest(plan=plan), self.assertRaises(renderer.ZuulConfigError):
                renderer.render(plan, 'docker')

    def test_local_checks_are_read_only_and_not_connection_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'config'
            provision(root)
            (root / 'zuul.conf').write_text('[connection "platform-static"]\ndriver = static\n', encoding='utf-8')
            before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            for role in preflight.ROLES:
                report = preflight.verify_inputs(environment(root), role, config_root=root)
                self.assertTrue(report['local_inputs_verified'])
                self.assertFalse(report['connections_verified'])
                self.assertFalse(report['sandbox_verified'])
                self.assertFalse(report['ci_verified'])
            self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_database_tls_downgrade_and_duplicate_parameters_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'config'
            provision(root)
            for mode in ('disable', 'prefer', 'require', 'verify-ca'):
                env = environment(root)
                env['ZUUL_DATABASE_URI'] = env['ZUUL_DATABASE_URI'].replace('verify-full', mode)
                with self.assertRaises(preflight.ZuulPreflightError):
                    preflight.verify_inputs(env, 'scheduler', config_root=root)
            env = environment(root)
            env['ZUUL_DATABASE_URI'] += '&sslmode=disable'
            with self.assertRaises(preflight.ZuulPreflightError):
                preflight.verify_inputs(env, 'scheduler', config_root=root)

    def test_launcher_rejects_missing_static_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'config'
            provision(root)
            (root / 'zuul.conf').write_text('[connection "platform-static"]\ndriver = gerrit\n', encoding='utf-8')
            with self.assertRaises(preflight.ZuulPreflightError):
                preflight.verify_inputs(environment(root), 'launcher', config_root=root)

    def test_missing_environment_and_key_inputs_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'config'
            provision(root)
            for key in preflight.ENVIRONMENT:
                env = environment(root)
                env.pop(key)
                with self.assertRaises(preflight.ZuulPreflightError):
                    preflight.verify_inputs(env, 'web', config_root=root)
            (root / 'private/build-ssh.key').unlink()
            self.assertTrue(preflight.verify_inputs(environment(root), 'scheduler', config_root=root)['local_inputs_verified'])
            with self.assertRaises(OSError):
                preflight.verify_inputs(environment(root), 'executor', config_root=root)
            (root / 'private/zk-ca.pem').write_bytes(b'')
            with self.assertRaises(preflight.ZuulPreflightError):
                preflight.verify_inputs(environment(root), 'merger', config_root=root)

    def test_cli_writes_only_new_private_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / 'plan.json'
            plan.write_text(json.dumps(fixture()), encoding='utf-8')
            output = root / 'private' / 'generated'
            args = ['--plan', str(plan), '--runtime', 'raw', '--output-dir', str(output)]
            with mock.patch.object(renderer, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(renderer.main(args), 0)
            self.assertFalse(json.loads(stream.getvalue())['deployed'])
            self.assertNotIn('review.example.com', stream.getvalue())
            original = (output / 'zuul.conf').read_bytes()
            with mock.patch.object(renderer, 'ROOT', root), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(renderer.main(args), 1)
            self.assertEqual((output / 'zuul.conf').read_bytes(), original)

    def test_preflight_errors_do_not_echo_private_environment(self):
        env = {key: 'private-fixture-do-not-echo' for key in preflight.ENVIRONMENT}
        env['REQUESTS_CA_BUNDLE'] = '/etc/zuul/private/gerrit-ca.pem'
        with mock.patch.dict(preflight.os.environ, env), contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(preflight.main(['--role', 'scheduler']), 1)
        self.assertNotIn('private-fixture-do-not-echo', error.getvalue())

    def test_planner_does_not_promote_zuul_configuration_to_working_ci(self):
        import plan_platform_stack as planner
        for runtime in planner.RUNTIMES:
            plan = planner.stack_plan('gerrit-zuul-argocd', runtime)
            adapter = next(a for a in plan['auxiliary_adapters'] if a['component'] == 'zuul-configuration')
            self.assertTrue(adapter['artifacts_available'])
            self.assertFalse(adapter['live_verified'])
            self.assertFalse(plan['deployment_ready'])
            self.assertIn('integration:zuul:' + runtime, plan['gaps'])


if __name__ == '__main__':
    unittest.main()
