#!/usr/bin/env python3
"""Shared original-cluster Kyverno installation and immutable policy evidence."""

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import time

import deploy_kyverno_installation as deploy
import render_kyverno_installation as renderer
from bounded_file import read_bounded_bytes
from ci_gitops_handoff import read_observation
from kyverno_policy_status import original_policy_ready
from strict_json import loads_strict_json
from tekton_forge_reporter import owned
from test_tekton_worker_runtime_fixture import stage_rke2_image, verify_archive


def unsigned_init_container(container,image):
    # Ordinary init containers cannot inherit application health probes/lifecycle.
    return {'name':'init','image':image,'command':['/bin/sh','-ec','true'],
        **{key:copy.deepcopy(container[key]) for key in ('securityContext','resources')}}


def observe_admission(read):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = read_observation(read)
        if result:
            if time.monotonic() >= deadline:
                raise TimeoutError("Late admission observation cannot qualify acceptance")
            return result
        time.sleep(1)
    raise TimeoutError("Admission observation deadline")


class AuthorityChecks:
    def refuse_unsigned_ephemeral(self, test, pod, container):
        metadata = pod['metadata']
        # JSON Patch binds the original Pod identity without carrying an old
        # resourceVersion across the admission-controller audit.
        patch = [
            {'op': 'test', 'path': '/metadata/uid', 'value': metadata['uid']},
            {'op': 'test', 'path': '/spec/containers', 'value': pod['spec']['containers']},
            {'op': 'add', 'path': '/spec/ephemeralContainers', 'value': [container]},
        ]
        self.refuse(test, patch, metadata['namespace'], 'unsigned-ephemeral', [
            'patch', 'pod/' + metadata['name'], '--namespace=' + metadata['namespace'],
            '--subresource=ephemeralcontainers', '--type=json', '--patch-file=/dev/stdin',
            '--dry-run=server',
        ])

    def install_authority(self,native,context,*,credential_name='signature-read'):
        test=native['test'];client=context['client'];root=context['root'];stage='import-images'
        artifacts=Path(os.environ['KYVERNO_ADMISSION_TEST_ARTIFACTS'])
        release=read_bounded_bytes(artifacts/'install.yaml',max_bytes=8*1024*1024)
        test.assertEqual(hashlib.sha256(release).hexdigest(),renderer.RELEASE_SHA)
        metadata=loads_strict_json(read_bounded_bytes(artifacts/'images.json',max_bytes=256*1024))
        entries=metadata['images'];test.assertEqual({e['tag'] for e in entries},set(renderer.IMAGES))
        print(json.dumps({'kyverno_admission_stage':stage}),flush=True)
        runtime = context.get('runtime', 'k3s')
        images = {}
        for item in entries:
            image = item['tag'].rsplit(':', 1)[0] + '@' + renderer.IMAGES[item['tag']]
            test.assertEqual(item['image'], image)
            archive = artifacts / 'images' / (image.rsplit('sha256:', 1)[1] + '.tar')
            digest = verify_archive(archive, image)
            test.assertEqual(digest, item['archive_sha256'])
            images[image] = archive, digest
        nodes = context['nodes'] if runtime == 'rke2' else context['nodes'][1:]
        for node in nodes:
            for image, (archive, digest) in images.items():
                if runtime == 'rke2':
                    stage_rke2_image(context, node, archive, image, digest)
                    continue
                path='/tmp/fixture-image-'+image.rsplit('sha256:',1)[1]+'.tar'
                context['upload'](node,archive,path)
                context['ssh'](node,'sudo /mnt/fixtures/k3s ctr --address /run/k3s/containerd/containerd.sock --namespace k8s.io images import --digests '+path,seconds=120)
                context['ssh'](node,'rm -- '+path)
            if runtime == 'rke2':
                continue  # RKE2 agents use their installed private-registry trust configuration.
            directory='/var/lib/fixture-k3s/agent/etc/containerd/certs.d/192.0.2.91:8443'
            context['ssh'](node,'sudo mkdir -p '+directory)
            context['ssh'](node,'sudo tee '+directory+'/ca.crt >/dev/null',payload=(native['root']/'ca.crt').read_text())
            hosts='server = "https://192.0.2.91:8443"\n[host."https://192.0.2.91:8443"]\n  capabilities = ["pull", "resolve"]\n  ca = "'+directory+'/ca.crt"\n'
            context['ssh'](node,'sudo tee '+directory+'/hosts.toml >/dev/null',payload=hosts)
        def create(obj):return loads_strict_json(client.call(['create','-f','-','-o','json'],payload=json.dumps(obj).encode()))
        namespace=create({'apiVersion':'v1','kind':'Namespace','metadata':{'name':'kyverno','labels':{'pod-security.kubernetes.io/enforce':'restricted'}}})
        public_ca=(native['root']/'ca.crt').read_text()
        trust=create({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'private-ca','namespace':'kyverno'},'immutable':True,'data':{'ca.crt':public_ca}})
        selected={'schema_version':1,'runtime':runtime,'management_target':context['target'],'namespace_uid':namespace['metadata']['uid'],
            'trust_configmap':{'name':'private-ca','uid':trust['metadata']['uid'],'ca_sha256':hashlib.sha256(public_ca.encode()).hexdigest()}}
        bundle=root/'kyverno-package';bundle.mkdir(mode=0o700)
        prepared=renderer.render(selected,release)
        for name,text in prepared.items():(bundle/name).write_text(text,encoding='utf-8',newline='\n')
        digest=loads_strict_json(prepared['kyverno-installation-status.json'])['contract_sha256']
        stage='install-three-replica-profile'
        print(json.dumps({'kyverno_admission_stage':stage}),flush=True)
        checked=deploy.operate(bundle,digest,check_server=True);test.assertFalse(checked['deployed'])
        installed=deploy.operate(bundle,digest,apply=True);test.assertTrue(installed['controller_deployments_ready'])
        if credential_name is not None:
            self.create_signature_credential(context,credential_name)
        return selected,installed,digest

    def create_signature_credential(self,context,name):
        ca_secret=loads_strict_json((self.signature_reader/'credential').read_bytes())
        credential={'auths':{ca_secret['registry']:{'registrytoken':ca_secret['token']}}}
        obj={'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':'kyverno'},'immutable':True,'type':'kubernetes.io/dockerconfigjson',
            'data':{'.dockerconfigjson':base64.b64encode(json.dumps(credential).encode()).decode()}}
        context['client'].call(['create','-f','-','-o','name'],payload=json.dumps(obj).encode())

    def compile_authority_policies(self,native,context,policy,namespaces,*,wrong_bindings=None,credential_name='signature-read'):
        test=native['test'];client=context['client']
        def create(obj):return loads_strict_json(client.call(['create','-f','-','-o','json'],payload=json.dumps(obj).encode()))
        policies=[]
        for index,name in enumerate(namespaces):
            chosen=copy.deepcopy(policy);chosen['metadata']['name']=name
            chosen['spec']['matchConstraints']['namespaceSelector']={'matchLabels':{'kubernetes.io/metadata.name':name}}
            chosen['spec']['matchConstraints']['resourceRules'][0]['resources']=['pods','pods/ephemeralcontainers']
            chosen['spec']['credentials']={'allowInsecureRegistry':False,'secrets':[credential_name]}
            if index==1:chosen['spec']['attestors'][0]['cosign']['key']['data']=(self.signature_reader/'wrong.pub').read_text()
            if wrong_bindings is not None and name in wrong_bindings or wrong_bindings is None and index==2:
                values=chosen['spec']['attestors'][0]['cosign']['annotations']
                key=wrong_bindings[name] if wrong_bindings is not None else next(iter(values))
                test.assertIn(key,values);test.assertNotEqual(values[key],'0'*64);values[key]='0'*64
            original=create(chosen);policies.append(original)
        def policy_ready():
            for item in policies:
                obj=client.get('imagevalidatingpolicy.policies.kyverno.io/'+item['metadata']['name'])
                if not original_policy_ready(obj,item):return False
            return True
        context['wait'](policy_ready,seconds=90)
        webhook_rows=loads_strict_json(client.call(['get','validatingwebhookconfigurations','-o','json']))['items']
        hooks=[(row,hook) for row in webhook_rows for hook in row.get('webhooks',[]) if hook.get('clientConfig',{}).get('service',{}).get('path','').startswith('/ivpol/') and hook.get('failurePolicy')=='Fail']
        test.assertTrue(hooks)
        for _,hook in hooks:
            test.assertEqual(hook['clientConfig']['service']['name'],'kyverno-svc')
            test.assertEqual(hook['clientConfig']['service']['namespace'],'kyverno')
            test.assertTrue(base64.b64decode(hook['clientConfig']['caBundle'],validate=True))
        return policies,webhook_rows

    def audit_authority(self,native,context,installed,namespaces,policies):
        return observe_admission(lambda: self._audit_authority(native,context,installed,namespaces,policies))

    def _audit_authority(self,native,context,installed,namespaces,policies):
        test=native['test'];client=context['client']
        controllers={name:client.get('deployment.apps/'+name,'kyverno') for name in renderer.CONTROLLERS}
        controller_pods=loads_strict_json(client.call(['get','pods','-n','kyverno','-o','json']))['items']
        for name,(replicas,cpu,memory,limit) in renderer.CONTROLLERS.items():
            live=controllers[name];expected={'requests':{'cpu':cpu,'memory':memory},'limits':{'memory':limit}}
            test.assertEqual(live['metadata']['uid'],installed['controller_uids'][name])
            test.assertEqual(live['spec']['replicas'],replicas)
            test.assertEqual(live['status']['observedGeneration'],live['metadata']['generation'])
            for field in ('replicas','readyReplicas','updatedReplicas','availableReplicas'):test.assertEqual(live['status'].get(field),replicas)
            test.assertEqual(live['spec']['template']['spec']['containers'][0]['resources'],expected)
            rows=[p for p in controller_pods if all(p['metadata'].get('labels',{}).get(k)==v for k,v in live['spec']['selector']['matchLabels'].items())]
            test.assertEqual(len(rows),replicas);test.assertEqual(len({p['spec']['nodeName'] for p in rows}),replicas)
            for row in rows:
                test.assertEqual(row['spec']['containers'][0]['resources'],expected)
                test.assertEqual(row['spec']['containers'][0]['image'],live['spec']['template']['spec']['containers'][0]['image'])
                test.assertTrue(row['status']['containerStatuses'][0]['imageID'].endswith('@'+row['spec']['containers'][0]['image'].split('@')[1]))
            test.assertTrue(all(p['status']['phase']=='Running' and all(s['ready'] for s in p['status']['containerStatuses']) for p in rows))
        replicasets=loads_strict_json(client.call(['get','replicasets','-n','kyverno','-o','json']))['items']
        pdbs=loads_strict_json(client.call(['get','poddisruptionbudgets','-n','kyverno','-o','json']))['items']
        for name,(replicas,_,_,_) in renderer.CONTROLLERS.items():
            controller=controllers[name]
            parents={r['metadata']['uid'] for r in replicasets if owned(r,'Deployment',controller['metadata']['uid'])}
            rows=[p for p in controller_pods if all(p['metadata'].get('labels',{}).get(k)==v for k,v in controller['spec']['selector']['matchLabels'].items())]
            for pod in rows:
                test.assertTrue(any(owned(pod,'ReplicaSet',parent) for parent in parents))
                test.assertFalse(pod['metadata'].get('deletionTimestamp'))
                test.assertEqual(len(pod['status']['containerStatuses']),len(pod['spec']['containers']))
                test.assertTrue(all(v.get('ready') is True and v.get('state',{}).get('running') for v in pod['status']['containerStatuses']))
            test.assertEqual(next(p for p in pdbs if p['metadata']['name']==name)['spec']['minAvailable'],replicas-1)
        for name,uid in namespaces.items():test.assertEqual(client.get('namespace/'+name)['metadata']['uid'],uid)
        for original in policies:test.assertTrue(original_policy_ready(client.get('imagevalidatingpolicy.policies.kyverno.io/'+original['metadata']['name']),original))
        return {'controllers':controllers,'controller_pods':controller_pods,
            'controller_replicasets':replicasets,'controller_disruption_budgets':pdbs,
            'policies':[client.get('imagevalidatingpolicy.policies.kyverno.io/'+p['metadata']['name']) for p in policies],
            'namespace_uids':dict(namespaces)}
