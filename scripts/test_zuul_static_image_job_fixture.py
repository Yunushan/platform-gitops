"""Opt-in actual encrypted Zuul job, persisted receipt and native Harbor content."""

import base64
import copy
import hashlib
import http.client
import io
import json
import os
import tarfile
import tempfile
from pathlib import Path
from unittest import mock
from urllib.parse import quote, urlsplit

import yaml

from atomic_file import atomic_write_text
from bounded_file import read_bounded_text
import render_zuul_static_image as renderer
from test_k3s_vm_fixture import PROBE_MANIFEST
from test_zuul_static_image import fixture
from test_zuul_static_image_harbor_fixture import StaticPublicationChecks
from test_zuul_web_fixture import wait, web_request
import zuul_static_image as worker


def project_key():
    """Read one public project key from the original disposable web endpoint."""
    connection = http.client.HTTPConnection('127.0.0.101', 9000, timeout=3)
    try:
        connection.request('GET', '/api/tenant/platform/key/127.0.0.1/platform/ci-config.pub')
        response = connection.getresponse()
        value = response.read(16385)
        if response.status != 200 or len(value) > 16384 or not value.startswith(b'-----BEGIN PUBLIC KEY-----'):
            raise ValueError('The original trusted project public encryption key is unavailable')
        return value
    finally:
        connection.close()


def encrypted_secret(name, payload, public_key):
    # Match the pinned upstream Zuul RSAES-OAEP/SHA1 format, including chunks
    # for PEM certificates and JSON. Never persist the project private key.
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    key = serialization.load_pem_public_key(public_key)
    if not isinstance(key, rsa.RSAPublicKey) or not 2048 <= key.key_size <= 8192:
        raise ValueError('Select the actual bounded Zuul RSA project key')
    width = key.key_size // 8 - 42
    class Ciphertext(list): pass
    class Dumper(yaml.SafeDumper): pass
    Dumper.add_representer(Ciphertext, lambda dumper, value: dumper.represent_sequence('!encrypted/pkcs1-oaep', value))
    def encrypt(value):
        value = value.encode()
        if not 1 <= len(value) <= 65536: raise ValueError('Bound disposable secret inputs')
        return Ciphertext(base64.b64encode(key.encrypt(value[index:index + width], padding.OAEP(
            mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))).decode()
            for index in range(0, len(value), width))
    return yaml.dump([{'secret': {'name': name, 'data': {key: encrypt(value) for key, value in payload.items()}}}], Dumper=Dumper, sort_keys=False)


def submit_files(api, project, files, subject):
    change = api.call('fixture-admin', '/a/changes/', {'project': project, 'branch': 'main', 'subject': subject, 'work_in_progress': False})
    number = str(change['_number'])
    for name, body in files.items():
        api.call('fixture-admin', '/a/changes/' + number + '/edit/' + quote(name, safe=''),
                 body.encode(), method='PUT', content_type='text/plain')
    api.call('fixture-admin', '/a/changes/' + number + '/edit:publish', {'notify': 'NONE'})
    api.call('fixture-admin', '/a/changes/' + number + '/revisions/current/review', {'labels': {'Code-Review': 2}, 'notify': 'NONE'})
    api.call('fixture-admin', '/a/changes/' + number + '/submit', {'wait_for_merge': True, 'notify': 'NONE'})
    return api.call('fixture-admin', '/a/projects/' + quote(project, safe='') + '/branches/main')['revision']


class JobPublicationChecks(StaticPublicationChecks):
    application_timeout_seconds = 1700

    def application_files(self, registry):
        return {'site/index.html': 'exact-source-v1\n'}

    def trusted_job(self, context, test, coordination, gerrit, actual_ci):
        api = gerrit['api']
        reader = api.bootstrap('ci-review')
        if not reader.get('username'):
            api.call('ci-review', '/a/accounts/self/username', {'username': 'ci-review'}, method='PUT')
        group = api.call('fixture-admin', '/a/groups/static-source-readers', {}, method='PUT')['id']
        api.call('fixture-admin', '/a/groups/' + quote(group, safe='') + '/members/' + str(reader['_account_id']), {}, method='PUT')
        api.call('fixture-admin', '/a/projects/applications%2Fexample/access', {'add': {'refs/heads/main': {
            'permissions': {'read': {'rules': {group: {'action': 'ALLOW'}}}}}}})
        caps = api.call('ci-review', '/a/accounts/self/capabilities')
        test.assertFalse(caps.get('administrateServer', False))
        # The synthetic plan factory creates a placeholder CA and credential.
        # Never give it the native Harbor directory where those names are live.
        with tempfile.TemporaryDirectory(prefix='platform-zuul-static-example-') as directory:
            plan, ci, _, _ = fixture(Path(directory))
        ci['gerrit'] = copy.deepcopy(actual_ci['gerrit'])
        ca = (context['root'] / 'ca.crt').read_bytes()
        test.assertEqual(hashlib.sha256(ca).hexdigest(), self.robot_plan['server']['ca_sha256'])
        credential = (self.robot_directory / 'credential.json').read_bytes()
        source_ca = (gerrit['tls'] / 'ca.crt').read_bytes()
        source_token = gerrit['users']['ci-review']
        origin = urlsplit(gerrit['origin'])
        plan['source'].update(clone_url=gerrit['origin'] + '/a/applications/example.git', read_username='ci-review',
                              ca_sha256=hashlib.sha256(source_ca).hexdigest())
        test.assertEqual(origin.hostname, ci['gerrit']['host'])
        plan['registry'].update(origin='https://192.0.2.91:8443', ca_sha256=hashlib.sha256(ca).hexdigest(),
            leaf_sha256=self.service_clients['harbor-core'].leaf, credential_sha256=hashlib.sha256(credential).hexdigest(),
            project_id=self.robot_plan['project']['id'], robot_id=self.robot_id, base_digest='sha256:' + PROBE_MANIFEST)
        files = renderer.render(plan, ci, 'raw')
        contract = json.loads(files[renderer.PACKAGE + '/contract.json'])
        payload = {'source_ca': source_ca.decode(), 'source_token': source_token,
                   'registry_ca': ca.decode(), 'registry_credential': credential.decode()}
        key = project_key()
        secret_yaml = encrypted_secret(plan['secret'], payload, key)
        private_values = (source_token, self.robot_credential['secret'])
        if any(secret in secret_yaml for secret in private_values):
            raise ValueError('A private value appeared in the trusted configuration')
        # Preserve the existing integrated provider/startup job while adding the
        # exact generated publisher bundle. Only ciphertext enters trusted Git.
        files['zuul.yaml'] = gerrit['zuul_startup_configuration'].decode() + '\n' + files['zuul.yaml'] + '\n' + secret_yaml
        del files['render-status.json']
        trusted_revision = submit_files(api, plan['config_project'], files, 'Disposable trusted encrypted publisher')
        def loaded():
            errors = web_request('127.0.0.101', '/api/tenant/platform/config-errors')
            if errors: raise ValueError('Trusted publisher reconfiguration failed; private diagnostic retained')
            return any(row['name'] == plan['pipeline'] for row in web_request('127.0.0.101', '/api/tenant/platform/pipelines'))
        wait(loaded, 90)
        revision = submit_files(api, plan['project'], self.application_files(plan['registry']), 'Disposable reviewed static source')
        def finished():
            for build in web_request('127.0.0.101', '/api/tenant/platform/builds'):
                if (build.get('job_name') == plan['job'] and build.get('pipeline') == plan['pipeline']
                        and build.get('ref', {}).get('newrev') == revision and build.get('final') is True):
                    row = web_request('127.0.0.101', '/api/tenant/platform/buildset/' + build['buildset']['uuid'])
                    if row.get('result'): return row
            return False
        buildset = wait(finished, 300)
        if any(secret in json.dumps(buildset) for secret in private_values):
            raise ValueError('A private value appeared in the persisted buildset')
        logs = {}
        for path in (coordination['root'] / 'zuul-executor-1-state/builds').glob('*/work/logs/job-output.*'):
            value = read_bounded_text(path, max_bytes=4 * 1024 ** 2)
            for secret in sorted([*payload.values(), *gerrit['users'].values(), *context['private_values']], key=len, reverse=True):
                if secret: value = value.replace(secret, '[redacted]')
            logs[path.parent.parent.parent.name + '/' + path.name] = value
        diagnostic = Path(os.environ['ZUUL_STATIC_JOB_DIAGNOSTIC']).resolve()
        private = renderer.ROOT / 'private/validation'
        if not diagnostic.is_relative_to(private.resolve()): raise ValueError('Keep scrubbed job diagnostics private')
        atomic_write_text(diagnostic, json.dumps({'buildset': buildset, 'logs': logs}, sort_keys=True) + '\n', replace=False)
        print(json.dumps({'zuul_static_job_result': buildset.get('result'), 'scrubbed_job_logs': len(logs)}), flush=True)
        from zuul_gitops_handoff import verify_buildset
        policy = {'tenant': plan['tenant'], 'pipeline': plan['pipeline'], 'project': plan['project'], 'branch': plan['branch'],
                  'revision': revision, 'uuid': buildset['uuid'], 'required_jobs': [plan['job']]}
        proof = verify_buildset(policy, lambda path: web_request('127.0.0.101', path))
        builds = [build for build in buildset['builds'] if build.get('job_name') == plan['job'] and build.get('final') is True]
        test.assertEqual(len(builds), 1)
        build = builds[0]
        event = {'build_uuid': build['uuid'], 'pipeline': plan['pipeline'], 'project': plan['project'], 'branch': plan['branch'], 'revision': revision}
        receipt = worker.verify_artifact(contract, event, build['artifacts'])
        # Base seeding and the job may be separated by several minutes. Start
        # an independent bounded TLS session for digest readback.
        verifier = worker.publisher.Registry(plan['registry'], ca, self.robot_credential)
        verifier.authenticate()
        manifest = json.loads(verifier.fetch('manifests', receipt['manifest_digest']))
        test.assertEqual(manifest['config']['digest'], receipt['config_digest'])
        test.assertEqual(manifest['layers'][-1]['digest'], receipt['output_layer_digest'])
        layer = verifier.fetch('blobs', receipt['output_layer_digest'])
        with tarfile.open(fileobj=io.BytesIO(layer), mode='r:') as archive:
            test.assertEqual(archive.extractfile('www/index.html').read(), b'exact-source-v1\n')
        public = json.dumps({'buildset': buildset, 'proof': proof})
        if any(secret in public or secret.encode() in layer for secret in private_values):
            raise ValueError('A private value appeared in persisted CI or OCI output')
        self.static_report = {'zuul_static_actual_encrypted_job_executed_verified': True,
            'zuul_static_persisted_artifact_metadata_verified': True, 'zuul_static_exact_source_oci_content_verified': True,
            'zuul_static_programs_sha256': contract['programs_sha256'], 'zuul_static_publication_sha256': {
                'contract': worker.fingerprint(contract), 'source_revision': hashlib.sha256(revision.encode()).hexdigest(),
                'trusted_revision': hashlib.sha256(trusted_revision.encode()).hexdigest(), 'buildset': worker.fingerprint(buildset),
                'project_public_key': hashlib.sha256(key).hexdigest(), 'manifest': receipt['manifest_digest'].removeprefix('sha256:')},
            'zuul_static_application_ci_verified': False, 'zuul_static_deployment_verified': False,
            'zuul_static_physical_ha_verified': False, 'zuul_static_full_stack_verified': False}
        if hasattr(self, 'rollout'):
            self.static_report.update(self.rollout(test, context, gerrit, policy, contract, receipt, ca))
            self.static_report['zuul_static_deployment_verified'] = True
        return self.static_report

    def jwt_workload_completed(self, context, phase):
        # Run the existing robot/core/registry checks without the CLI-only slice.
        super(StaticPublicationChecks, self).jwt_workload_completed(context, phase)
        if self.static_report is None and self.robot_phases == self.expected_jwt_phases:
            import test_zuul_web_fixture as web
            # Preserve the original failure-phase token windows. The nested
            # Zuul fixture uses different pinned PostgreSQL artifacts.
            with mock.patch.dict(os.environ):
                result = web.exercise(context['test'], integrated=True, executor_startup=True, real_job=True,
                    static_publication=lambda test, coordination, gerrit, ci: self.trusted_job(context, test, coordination, gerrit, ci))
            context['test'].assertTrue(result['actual_guarded_web_pair_verified'])

    def exercise(self, context):
        result = super(StaticPublicationChecks, self).exercise(context)
        if not self.static_report: raise ValueError('The actual trusted publisher job did not complete')
        result.update({'valkey_gateway_' + key: value for key, value in self.static_report.items()})
        return result


def exercise(test):
    if os.environ.get('ZUUL_STATIC_JOB_TEST_RUNTIME') != '1': raise ValueError('Explicitly select actual encrypted job execution')
    from test_harbor_pool_health_fixture import PoolHealthChecks
    from test_harbor_jobservice_replica_fixture import JobReplicaChecks
    from test_harbor_core_replica_fixture import CoreReplicaChecks
    from test_harbor_jwt_runtime_fixture import JWTChecks
    from test_harbor_database_promotion_fixture import DatabasePromotionChecks
    from test_harbor_service_runtime_fixture import ServiceChecks
    from test_harbor_registry_runtime_fixture import RegistryChecks
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    checks = JobPublicationChecks
    rollout = os.environ.get('ZUUL_ARTIFACT_ROLLOUT_TEST_RUNTIME') == '1'
    if os.environ.get('ZUUL_ADMITTED_ARTIFACT_ROLLOUT_TEST_RUNTIME') == '1':
        if not rollout or os.environ.get('ZUUL_SIGNED_ARTIFACT_ROLLOUT_TEST_RUNTIME') != '1':
            raise ValueError('Admission requires the explicit signed Zuul rollout fixture')
        from test_zuul_artifact_rollout_fixture import AdmittedRolloutChecks
        checks = AdmittedRolloutChecks
    elif rollout:
        from test_zuul_artifact_rollout_fixture import RolloutChecks
        checks = RolloutChecks
    class Application(checks, PoolHealthChecks, JobReplicaChecks, CoreReplicaChecks, JWTChecks, DatabasePromotionChecks, ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
