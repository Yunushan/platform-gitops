#!/usr/bin/env python3
"""Binding-specific admission policies retain strict trust and original identities."""

import base64
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_kyverno_admission_authority_fixture import (
    AuthorityChecks,
    observe_admission,
    unsigned_init_container,
)


class Client:
    def __init__(self):self.policies={};self.changed=False
    def call(self,args,*,payload=None):
        if args[0]=='create':
            value=json.loads(payload);name=value['metadata']['name']
            value['metadata'].update(uid='original-'+name,generation=1)
            self.policies[name]=value
            return json.dumps(value)
        return json.dumps({'items':[{'webhooks':[{'failurePolicy':'Fail','clientConfig':{'service':{'name':'kyverno-svc','namespace':'kyverno','path':'/ivpol/test'},'caBundle':base64.b64encode(b'public-ca').decode()}}]}]})
    def get(self,resource):
        value=copy.deepcopy(self.policies[resource.split('/')[1]])
        if self.changed:value['metadata']['uid']='replacement'
        value['status']={'conditionStatus':{'ready':True}}
        return value


class AuthorityContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);(root/'wrong.pub').write_text('different-public-key')
        self.authority=AuthorityChecks();self.authority.signature_reader=root
        self.client=Client()
        def wait(fn,*,seconds):
            if not fn():raise ValueError('Original policy is not ready')
        self.context={'client':self.client,'wait':wait};self.native={'test':self}
        self.annotations={'binding'+str(i):str(i)*64 for i in range(1,9)}
        self.policy={'metadata':{'name':'release'},'spec':{'failurePolicy':'Fail','validationActions':['Deny'],
            'matchConstraints':{'resourceRules':[{'resources':['pods']}]},
            'attestors':[{'cosign':{'key':{'data':'original-public-key'},'annotations':self.annotations}}]}}

    def test_admission_observation_retries_only_transport_reads_within_deadline(self):
        from kubernetes_management import KubernetesOperationError
        timeout = "Unable to connect to the server: net/http: request canceled (Client.Timeout exceeded while awaiting headers)"
        expected = {'original_uid': 'original-controller'}
        read = mock.Mock(side_effect=[KubernetesOperationError(1, timeout), expected])
        module = 'test_kyverno_admission_authority_fixture'
        with mock.patch(module + '.time.monotonic', side_effect=[0, 1, 2, 3]), mock.patch(module + '.time.sleep'):
            self.assertIs(observe_admission(read), expected)
        self.assertEqual(read.call_count, 2)
        for failure in (KubernetesOperationError(1, timeout + " Forbidden"),
                        KubernetesOperationError(1, timeout + " x509: certificate signed by unknown authority"),
                        ValueError('Original identity changed'), AssertionError('Policy denial missing')):
            read = mock.Mock(side_effect=failure)
            with self.subTest(failure=type(failure).__name__), self.assertRaises(type(failure)):
                observe_admission(read)
            read.assert_called_once()
        with mock.patch(module + '.time.monotonic', side_effect=[0, 1, 91]):
            with self.assertRaisesRegex(TimeoutError, 'Late admission observation'):
                observe_admission(mock.Mock(return_value=expected))
        read = mock.Mock(side_effect=KubernetesOperationError(1, timeout))
        with mock.patch(module + '.time.monotonic', side_effect=[0, 1, 91]), mock.patch(module + '.time.sleep'):
            with self.assertRaisesRegex(TimeoutError, 'Admission observation deadline'):
                observe_admission(read)
        read.assert_called_once()

    def test_ephemeral_denial_targets_original_pod_without_stale_resource_version(self):
        pod = {'metadata': {'name': 'web', 'namespace': 'application',
                            'uid': 'original-pod', 'resourceVersion': 'stale'},
               'spec': {'containers': [{'name': 'web', 'image': 'signed-image'}]}}
        original = copy.deepcopy(pod)
        container = {'name': 'unsigned-debug', 'image': 'unsigned-image'}
        with mock.patch.object(self.authority, 'refuse', create=True) as refuse:
            self.authority.refuse_unsigned_ephemeral(self, pod, container)
        _, patch, namespace, case, args = refuse.call_args.args
        self.assertEqual(patch, [
            {'op': 'test', 'path': '/metadata/uid', 'value': 'original-pod'},
            {'op': 'test', 'path': '/spec/containers', 'value': pod['spec']['containers']},
            {'op': 'add', 'path': '/spec/ephemeralContainers', 'value': [container]},
        ])
        self.assertEqual((namespace, case), ('application', 'unsigned-ephemeral'))
        self.assertEqual(args, [
            'patch', 'pod/web', '--namespace=application', '--subresource=ephemeralcontainers',
            '--type=json', '--patch-file=/dev/stdin', '--dry-run=server',
        ])
        self.assertEqual(pod, original)
        with mock.patch.object(self.authority, 'refuse', create=True,
                               side_effect=AssertionError('Named policy denial missing')):
            with self.assertRaisesRegex(AssertionError, 'Named policy denial missing'):
                self.authority.refuse_unsigned_ephemeral(self, pod, container)

    def test_unsigned_init_probe_uses_valid_ordinary_init_fields(self):
        container={'name':'web','image':'signed-image','command':['httpd'],'args':['-f'],
            'readinessProbe':{'httpGet':{'path':'/','port':8080}},'livenessProbe':{},'startupProbe':{},'lifecycle':{},'ports':[{'containerPort':8080}],
            'securityContext':{'runAsNonRoot':True},'resources':{'requests':{'memory':'32Mi'}}}
        original=copy.deepcopy(container);init=unsigned_init_container(container,'unsigned-image')
        self.assertEqual(set(init),{'name','image','command','securityContext','resources'})
        self.assertEqual(init['image'],'unsigned-image');self.assertEqual(init['command'],['/bin/sh','-ec','true'])
        self.assertEqual(init['securityContext'],container['securityContext']);self.assertEqual(container,original)
        init['resources']['requests']['memory']='64Mi';self.assertEqual(container,original)

    def test_all_eight_negative_policies_change_only_selected_binding(self):
        original=copy.deepcopy(self.policy)
        wrong={'bad-'+str(i):key for i,key in enumerate(self.annotations)}
        names={'application':'original-app','bad-key':'original-key',**dict.fromkeys(wrong,'original-namespace')}
        policies,_=self.authority.compile_authority_policies(self.native,self.context,self.policy,names,wrong_bindings=wrong)
        self.assertEqual(len(policies),10);self.assertEqual(self.policy,original)
        for value in policies:
            name=value['metadata']['name'];spec=value['spec'];cosign=spec['attestors'][0]['cosign']
            self.assertEqual(spec['failurePolicy'],'Fail');self.assertEqual(spec['validationActions'],['Deny'])
            self.assertEqual(spec['matchConstraints']['namespaceSelector'],{'matchLabels':{'kubernetes.io/metadata.name':name}})
            self.assertEqual(spec['matchConstraints']['resourceRules'][0]['resources'],['pods','pods/ephemeralcontainers'])
            self.assertEqual(spec['credentials'],{'allowInsecureRegistry':False,'secrets':['signature-read']})
            expected={**self.annotations,**({wrong[name]:'0'*64} if name in wrong else {})}
            self.assertEqual(cosign['annotations'],expected)
            self.assertEqual(cosign['key']['data'],'different-public-key' if name=='bad-key' else 'original-public-key')

    def test_default_three_policy_cases_remain_available(self):
        policies,_=self.authority.compile_authority_policies(self.native,self.context,self.policy,{'app':'1','key':'2','binding':'3'})
        self.assertEqual(policies[0]['spec']['attestors'][0]['cosign']['annotations'],self.annotations)
        self.assertEqual(policies[2]['spec']['attestors'][0]['cosign']['annotations']['binding1'],'0'*64)

    def test_replacement_policy_cannot_satisfy_ready_gate(self):
        self.client.changed=True
        with self.assertRaisesRegex(ValueError,'Original.*policy'):
            self.authority.compile_authority_policies(self.native,self.context,self.policy,{'app':'1'})

    def test_unknown_signed_binding_is_rejected(self):
        with self.assertRaises(AssertionError):
            self.authority.compile_authority_policies(self.native,self.context,self.policy,{'app':'1','key':'2','binding':'3'},wrong_bindings={'binding':'unreviewed'})


class OriginalWorkerContracts(unittest.TestCase):
    def fixture(self):
        return {'admission_workers':True,'nodes':[
            {'name':'fixture-control','address':'192.0.2.2','memory':1536},
            {'name':'fixture-worker-1','address':'192.0.2.3','memory':2048},
            {'name':'fixture-worker-2','address':'192.0.2.4','memory':2048},
            {'name':'fixture-worker-3','address':'192.0.2.7','memory':2048}]}

    def test_three_workers_require_explicit_original_inventory(self):
        from test_gerrit_review_runtime_fixture import worker_inventory
        context=self.fixture();self.assertEqual(worker_inventory(context),context['nodes'])
        for mode in ('absent','false','string','memory','address','extra-control','missing-worker','native-edge'):
            changed=copy.deepcopy(context)
            if mode=='absent':changed.pop('admission_workers')
            elif mode=='false':changed['admission_workers']=False
            elif mode=='string':changed['admission_workers']='true'
            elif mode=='memory':changed['nodes'][-1]['memory']=1024
            elif mode=='address':changed['nodes'][-1]['address']='192.0.2.8'
            elif mode=='extra-control':changed['control_nodes']=[context['nodes'][0],{'name':'fixture-control-2','address':'192.0.2.5'}]
            elif mode=='missing-worker':changed['nodes'].pop()
            else:changed['native_edge_hosts']={}
            with self.subTest(mode=mode),self.assertRaises(ValueError):worker_inventory(changed)

    def test_default_two_workers_remain_exact(self):
        from test_gerrit_review_runtime_fixture import worker_inventory
        context=self.fixture();context.pop('admission_workers');context['nodes'].pop()
        self.assertEqual(worker_inventory(context),context['nodes'])


class OriginalApplicationFailureContracts(unittest.TestCase):
    def test_only_exact_original_webhook_eof_can_enter_dry_run_convergence(self):
        from test_kyverno_failure_fixture import named_webhook_eof
        message='Error from server (InternalError): failed calling webhook "original-hook": failed to call webhook: Post "https://kyverno-svc.kyverno.svc:443/ivpol/validate/original-policy?timeout=10s": EOF\n'
        row={'returncode':1,'stdout':'','stderr':message}
        self.assertTrue(named_webhook_eof(row,'original-hook','original-policy'))
        for changed in ({**row,'returncode':0},{**row,'returncode':None},{**row,'stdout':'unexpected object'},
                {**row,'stderr':message.replace('original-hook','other-hook')},{**row,'stderr':message.replace('original-policy','other-policy')},
                *({**row,'stderr':message.replace('failed calling webhook',value+' failed calling webhook')} for value in ('denied the request','x509','certificate','Forbidden','Unauthorized','Invalid value','TLS handshake')),
                {**row,'stderr':message.replace('EOF','context deadline exceeded')}):
            self.assertFalse(named_webhook_eof(changed,'original-hook','original-policy'))

    def test_signed_transport_retry_is_bounded_and_late_or_unsafe_results_fail(self):
        from unittest import mock

        from test_kyverno_failure_fixture import converge_signed_dry_run
        message='Error from server (InternalError): failed calling webhook "hook": failed to call webhook: Post "https://kyverno-svc.kyverno.svc:443/ivpol/validate/policy?timeout=10s": EOF'
        failed={'returncode':1,'stdout':'','stderr':message};success={'returncode':0,'stdout':'accepted','stderr':''}
        rows=[];now=[0.0]
        def perform(attempt,seconds):
            self.assertLessEqual(seconds,45);row=dict(failed if attempt==1 else success);rows.append(row);return row
        result=converge_signed_dry_run(perform,lambda:True,'hook','policy',100,clock=lambda:now[0],pause=lambda seconds:now.__setitem__(0,now[0]+seconds))
        self.assertEqual(result['returncode'],0);self.assertEqual(len(rows),2);self.assertTrue(rows[0]['transport_retry']);self.assertEqual(rows[0]['transport_attempt'],1)
        always=mock.Mock(side_effect=lambda *args:dict(failed))
        self.assertEqual(converge_signed_dry_run(always,lambda:True,'hook','policy',100,clock=lambda:0,pause=lambda _:None)['returncode'],1);self.assertEqual(always.call_count,3)
        unsafe=mock.Mock(return_value={**failed,'stderr':'denied the request: EOF'})
        self.assertEqual(converge_signed_dry_run(unsafe,lambda:True,'hook','policy',100,clock=lambda:0,pause=lambda _:None)['returncode'],1);unsafe.assert_called_once()
        untouched=mock.Mock()
        with self.assertRaises(ValueError):converge_signed_dry_run(untouched,lambda:False,'hook','policy',100,clock=lambda:0)
        untouched.assert_not_called()
        def late(*args):now[0]=101;return dict(success)
        now[0]=0
        with self.assertRaises(TimeoutError):converge_signed_dry_run(late,lambda:True,'hook','policy',100,clock=lambda:now[0])

    def fixture(self,name='artifact-web',namespace='synthetic-application'):
        image='registry.example.invalid/image@sha256:'+'a'*64;selector={'app':name}
        workload={'kind':'Deployment','metadata':{'name':name,'namespace':namespace,'uid':'original-deployment'},
            'spec':{'replicas':3,'selector':{'matchLabels':selector},'template':{'spec':{'containers':[{'image':image}]}}}}
        service={'kind':'Service','metadata':{'name':name,'namespace':namespace,'uid':'original-service'},'spec':{'selector':selector.copy()}}
        pods=[{'metadata':{'namespace':namespace,'uid':'original-pod-'+str(i),'labels':selector.copy()},'spec':{'containers':[{'image':image}]}} for i in range(3)]
        policy={'metadata':{'name':'release-policy','uid':'original-policy','generation':1},'spec':{'failurePolicy':'Fail','validationActions':['Deny'],
            'matchConstraints':{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':namespace}}}}}
        signed={'metadata':{'namespace':namespace},'spec':{'containers':[{'image':image}]}}
        unsigned=copy.deepcopy(signed);unsigned['spec']['containers'][0]['image']='registry.example.invalid/unsigned@sha256:'+'b'*64
        return {'workload':workload,'service':service,'pods':pods,'namespace_uids':{namespace:'original-namespace'}},[policy],signed,unsigned

    def test_fault_scope_uses_original_argo_workload_and_policy_identities(self):
        from test_kyverno_failure_fixture import application_scope
        baseline,policies,signed,unsigned=self.fixture();before=copy.deepcopy((baseline,policies,signed,unsigned))
        scope=application_scope(baseline,policies,signed,unsigned)
        self.assertEqual(scope,{'namespace':'synthetic-application','namespace_uid':'original-namespace','deployment':'artifact-web','deployment_uid':'original-deployment',
            'service':'artifact-web','service_uid':'original-service','selector':'app=artifact-web','policy_name':'release-policy','policy_uid':'original-policy','policy_generation':1,
            'pod_uids':['original-pod-0','original-pod-1','original-pod-2']})
        self.assertEqual((baseline,policies,signed,unsigned),before)

    def test_existing_synthetic_fault_workload_remains_supported(self):
        from test_kyverno_failure_fixture import application_scope
        scope=application_scope(*self.fixture('signed-web','admission-fixture'))
        self.assertEqual(scope['deployment'],'signed-web');self.assertEqual(scope['namespace'],'admission-fixture')

    def test_unrelated_workloads_unsigned_aliases_and_weakened_policies_refuse(self):
        from test_kyverno_failure_fixture import application_scope
        for mode in ('service-namespace','service-selector','probe-namespace','signed-image','unsigned-image','pod-image','pod-namespace','duplicate-pod','replicas','policy-namespace','ambiguous-policy','audit','ignore','missing-uid'):
            baseline,policies,signed,unsigned=self.fixture()
            if mode=='service-namespace':baseline['service']['metadata']['namespace']='unrelated'
            elif mode=='service-selector':baseline['service']['spec']['selector']={'app':'unrelated'}
            elif mode=='probe-namespace':signed['metadata']['namespace']='unrelated'
            elif mode=='signed-image':signed['spec']['containers'][0]['image']='unrelated'
            elif mode=='unsigned-image':unsigned=copy.deepcopy(signed)
            elif mode=='pod-image':baseline['pods'][0]['spec']['containers'][0]['image']='unrelated'
            elif mode=='pod-namespace':baseline['pods'][0]['metadata']['namespace']='unrelated'
            elif mode=='duplicate-pod':baseline['pods'][1]=copy.deepcopy(baseline['pods'][0])
            elif mode=='replicas':baseline['workload']['spec']['replicas']=2
            elif mode=='policy-namespace':policies[0]['spec']['matchConstraints']['namespaceSelector']['matchLabels']['kubernetes.io/metadata.name']='unrelated'
            elif mode=='ambiguous-policy':policies.append(copy.deepcopy(policies[0]))
            elif mode=='audit':policies[0]['spec']['validationActions']=['Audit']
            elif mode=='ignore':policies[0]['spec']['failurePolicy']='Ignore'
            else:policies[0]['metadata']['uid']=''
            with self.subTest(mode=mode),self.assertRaises(ValueError):application_scope(baseline,policies,signed,unsigned)

    def test_extended_runtime_requires_explicit_profile_and_remains_bounded(self):
        from test_k3s_vm_fixture import guest_runtime_profile
        self.assertEqual(guest_runtime_profile(1100),1100)
        self.assertEqual(guest_runtime_profile(1700,admission_workers=True),1700)
        self.assertEqual(guest_runtime_profile(1700,quorum=True,network_bootstrap=lambda:None),1700)
        for seconds,options in ((1700,{}),(1700,{'quorum':True}),(1700,{'network_bootstrap':lambda:None}),
                (True,{}),(0,{}),(1101,{}),(1701,{}),(1700,{'admission_workers':'true'})):
            with self.subTest(seconds=seconds,options=list(options)),self.assertRaises(ValueError):guest_runtime_profile(seconds,**options)


class FixtureIdentityContracts(unittest.TestCase):
    def test_watch_audit_binds_poll_totals_to_exact_retained_log_bytes(self):
        import hashlib
        import secrets
        from unittest import mock

        from tekton_gerrit_bridge import FALSE_FLAGS
        from test_gerrit_native_watch_fixture import WatchPair

        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);pair=WatchPair.__new__(WatchPair);pair.test=self;pair.inputs={};pair.handles={};pair.runtime_limit=1700;pair.owner=None
            credential=secrets.token_urlsafe(32)
            for index,kind in enumerate(('bridge','reporter')):
                folder=root/kind;folder.mkdir();pair.inputs[kind]=folder
                (folder/'api-token').write_text(credential,encoding='utf-8')
                (folder/'kubeconfig').write_text(json.dumps({'users':[{'user':{'token':credential}}]}),encoding='utf-8')
                counts={'checked':1,'accepted':1,'duplicates':0} if kind=='bridge' else {'pending':0,'reported':0,'already-reported':1,'rejected':0}
                payload=(json.dumps({**counts,**dict.fromkeys(FALSE_FLAGS,False)})+'\n').encode();log=folder/'watch.log';log.write_bytes(payload)
                pair.handles[kind]={'name':'watch-'+kind,'uid':60001+index,'invocation':'a'*32,'digest':'b'*64,'args':[],'fragment':(1,'c'*64),'processes':[],'log':log}
            pair.inspect=mock.Mock();pair.show=mock.Mock(return_value={'NRestarts':'1','RuntimeMaxUSec':'28min 20s'})
            # A second read could include a newly appended poll. Counts must
            # describe the same captured bytes, not that later file state.
            pair.reports=mock.Mock(side_effect=AssertionError('unexpected second log read'))
            result=pair.audit([credential]);pair.reports.assert_not_called()
            self.assertEqual(result['bridge']['report_totals']['accepted'],1)
            self.assertEqual(result['reporter']['report_totals']['already-reported'],1)
            for kind,row in result.items():
                self.assertEqual(row['poll_log_sha256'],hashlib.sha256(row['poll_log'].encode()).hexdigest())
                self.assertEqual(row['poll_log'],pair.handles[kind]['log'].read_bytes().decode())

    def test_fresh_poll_budget_uses_exact_authenticated_role_contract(self):
        import hashlib
        import json
        from tempfile import TemporaryDirectory

        from test_gerrit_native_watch_fixture import fresh_poll_contract
        with TemporaryDirectory() as directory:
            root=Path(directory)
            for kind,name,interval in (('bridge','gerrit-event-contract.json',30),('reporter','gerrit-report-contract.json',10)):
                payload=json.dumps({'plan':{'poll_seconds':interval}}).encode();path=root/name;path.write_bytes(payload)
                digest=hashlib.sha256(payload).hexdigest();profile=fresh_poll_contract(kind,root,digest)
                self.assertEqual(profile['wait_budget_seconds'],3*interval);self.assertEqual(profile['required_records'],2)
                self.assertEqual(profile['contract_text'].encode(),payload)
                path.write_bytes(payload+b' ')
                with self.assertRaises(ValueError):fresh_poll_contract(kind,root,digest)
                for invalid in (True,False,0,9,301,'30',None):
                    raw=json.dumps({'plan':{'poll_seconds':invalid}}).encode();path.write_bytes(raw)
                    with self.assertRaises(ValueError):fresh_poll_contract(kind,root,hashlib.sha256(raw).hexdigest())
            with self.assertRaises(ValueError):fresh_poll_contract('unknown',root,'a'*64)

    def test_two_fresh_bridge_polls_follow_real_interval_and_late_success_fails(self):
        import hashlib
        import json
        from tempfile import TemporaryDirectory
        from unittest import mock

        import test_gerrit_native_watch_fixture as fixture
        from tekton_gerrit_bridge import FALSE_FLAGS
        row={**dict.fromkeys(('checked','accepted','duplicates'),0),**dict.fromkeys(FALSE_FLAGS,False)}
        with TemporaryDirectory() as directory:
            root=Path(directory);payload=json.dumps({'plan':{'poll_seconds':30}}).encode();(root/'gerrit-event-contract.json').write_bytes(payload)
            pair=fixture.WatchPair.__new__(fixture.WatchPair);pair.bundles={'bridge':root};pair.handles={'bridge':{'digest':hashlib.sha256(payload).hexdigest()}}
            pair.reports=mock.Mock(side_effect=[[row],[row,row],[row,row,row]])
            now=[0.0]
            def wait(predicate,seconds):
                self.assertEqual(seconds,90);now[0]=30;self.assertFalse(predicate());now[0]=60;self.assertTrue(predicate())
            pair.context={'wait':wait}
            with mock.patch.object(fixture.time,'monotonic',side_effect=lambda:now[0]):result=pair.fresh_poll('bridge')
            self.assertTrue(result['completed']);self.assertEqual([v['index'] for v in result['records']],[1,2]);self.assertEqual(result['observed_monotonic'],60)
            self.assertGreater(result['observed_monotonic']-result['fence_monotonic'],45)
            pair.reports=mock.Mock(side_effect=[[row],[row,row,row]]);now[0]=0
            def late(predicate,seconds):now[0]=91;predicate()
            pair.context={'wait':late}
            with mock.patch.object(fixture.time,'monotonic',side_effect=lambda:now[0]),self.assertRaises(TimeoutError):pair.fresh_poll('bridge')
            self.assertFalse(pair.fresh_poll_observations['bridge']['completed'])
            self.assertEqual(pair.fresh_poll_observations['bridge']['observed_monotonic'],91)

    def test_native_watch_lifetime_is_finite_and_default_unchanged(self):
        from test_gerrit_native_watch_fixture import (
            runtime_limit_microseconds,
            watch_runtime_limit,
        )
        self.assertEqual(watch_runtime_limit({}),300)
        self.assertEqual(watch_runtime_limit({'guest_runtime_limit_seconds':1100}),300)
        self.assertEqual(watch_runtime_limit({'guest_runtime_limit_seconds':1700}),1700)
        for text,expected in (('5min',300000000),('28min 20s',1700000000),('1700s',1700000000),('1700000000us',1700000000)):
            self.assertEqual(runtime_limit_microseconds(text),expected)
        for value in (True,'1700',0,1701):
            with self.subTest(value=value),self.assertRaises(ValueError):watch_runtime_limit({'guest_runtime_limit_seconds':value})
        for text in ('infinity','-5min','nan','1.5h','5min;command',None):
            with self.subTest(text=text),self.assertRaises(ValueError):runtime_limit_microseconds(text)

    def test_extended_watch_owner_refuses_unrelated_or_ambient_services(self):
        from test_gerrit_native_watch_fixture import owner_unit_name
        value='0::/system.slice/platform-k3s-vm-synthetic-123.service\n'
        self.assertEqual(owner_unit_name(value),('platform-k3s-vm-synthetic-123.service','/system.slice/platform-k3s-vm-synthetic-123.service'))
        for value in ('0::/','0::/system.slice/production.service','0::/system.slice/platform-k3s-vm-x.service/child','1:name=systemd:/system.slice/platform-k3s-vm-x.service','0::/system.slice/platform-k3s-vm-x.service\n0::/other'):
            with self.subTest(value=value),self.assertRaises(ValueError):owner_unit_name(value)

    def test_fresh_poll_records_require_real_complete_nonnegative_summaries(self):
        from tekton_gerrit_bridge import FALSE_FLAGS
        from test_gerrit_native_watch_fixture import successful_poll_rows
        for kind,fields in (('bridge',('checked','accepted','duplicates')),('reporter',('pending','reported','already-reported','rejected'))):
            row={**dict.fromkeys(fields,0),**dict.fromkeys(FALSE_FLAGS,False)}
            rows=[row,{'gerrit_reporter_failure':{'exception_type':'ValueError'}},row]
            self.assertEqual([v['index'] for v in successful_poll_rows(kind,rows,1)],[2])
            for change in ({fields[0]:True},{fields[0]:-1},{'full_stack_verified':True},{'unexpected':0}):
                self.assertEqual(successful_poll_rows(kind,[{**row,**change}],0),[])
            with self.assertRaises(ValueError):successful_poll_rows(kind,rows,True)
        with self.assertRaises(ValueError):successful_poll_rows('other',[],0)

    def test_credential_lifetime_covers_only_explicit_extended_guest_profile(self):
        from test_k3s_vm_fixture import guest_identity_lifetime
        self.assertEqual(guest_identity_lifetime(),'10m')
        self.assertEqual(guest_identity_lifetime({}),'10m')
        self.assertEqual(guest_identity_lifetime({'guest_runtime_limit_seconds':1100}),'10m')
        self.assertEqual(guest_identity_lifetime({'guest_runtime_limit_seconds':1700}),'30m')
        for value in (True,False,'1700',0,1101,1701):
            with self.subTest(value=value),self.assertRaises(ValueError):guest_identity_lifetime({'guest_runtime_limit_seconds':value})

    def test_gerrit_bootstrap_requests_finite_profile_and_keeps_same_credential(self):
        import secrets
        import ssl
        from types import SimpleNamespace
        from unittest import mock

        import test_gerrit_review_runtime_fixture as fixture
        for lifetime in ('10m','30m'):
            users={'reader':'initial-test-credential'};credentials={}
            api=fixture.API('https://example.invalid',ssl.create_default_context(),users,credentials,token_lifetime=lifetime)
            opener=mock.MagicMock();token=secrets.token_urlsafe(32)
            with mock.patch.object(fixture,'CookieJar',return_value=[SimpleNamespace(name='XSRF_TOKEN',value='test-xsrf')]),mock.patch.object(fixture,'build_opener',return_value=opener),mock.patch.object(fixture,'read_bounded_response',side_effect=[b'',b")]}'\n"+json.dumps({'token':token}).encode()]),mock.patch.object(api,'call',return_value={'id':1}):
                self.assertEqual(api.bootstrap('reader'),{'id':1})
            request=opener.open.call_args_list[1].args[0]
            self.assertEqual(json.loads(request.data),{'lifetime':lifetime})
            self.assertEqual(users['reader'],token);self.assertEqual(len(credentials),1)
        with self.assertRaises(ValueError):fixture.API('https://example.invalid',ssl.create_default_context(),{}, {},token_lifetime='unlimited')


@unittest.skipUnless(sys.platform=='linux','The admitted native fixture imports Linux signing identities')
class JoinedWatchModeContracts(unittest.TestCase):
    def test_invalid_profiles_stop_before_runtime_construction(self):
        from test_gerrit_admitted_promotion_fixture import exercise
        for options in ({'native_watch':'true'},{'admission_failures':'true'},{'native_watch':1},{'native_watch':True,'admission_failures':False}):
            with self.subTest(options=options),self.assertRaises(ValueError):exercise(self,**options)


@unittest.skipUnless(sys.platform=='linux','Original bounded log witness process identities use Linux /proc')
class OriginalLogStreamContracts(unittest.TestCase):
    def stream(self,program,**options):
        from test_kyverno_log_witness_fixture import BoundedLogStream
        return BoundedLogStream([sys.executable,'-u','-c',program],[],**options)

    def test_completed_original_stream_preserves_bytes_and_removes_process(self):
        stream=self.stream("print('leadership lost, stopped leading',flush=True)",seconds=2)
        stream.child.wait(timeout=2);result=stream.close()
        self.assertEqual(result['returncode'],0);self.assertIsNone(result['capture_error'])
        self.assertEqual(result['stdout'],'leadership lost, stopped leading\n');self.assertTrue(result['original_process_removed'])

    def test_output_limit_and_deadline_stop_original_processes(self):
        for program,options,error in (("import time;print('x'*50000,flush=True);time.sleep(20)",{'max_bytes':1024},'output_limit'),
                ("import time;print('ready',flush=True);time.sleep(20)",{'seconds':0.2},'capture_deadline')):
            stream=self.stream(program,**options)
            stream.child.wait(timeout=3);result=stream.close()
            self.assertEqual(result['capture_error'],error);self.assertNotEqual(result['returncode'],0)
            self.assertLessEqual(len(result['stdout'].encode())+len(result['stderr'].encode()),options.get('max_bytes',1024*1024))
            self.assertTrue(result['original_process_removed'])

    def test_failed_or_foreign_stream_cannot_supply_original_container_witness(self):
        from test_kyverno_log_witness_fixture import OriginalLogPool
        pool=OriginalLogPool(self,{},[])
        for code in (0,1):
            stream=self.stream("print('leadership lost, stopped leading',flush=True);raise SystemExit("+str(code)+")",seconds=2)
            pool.entries.append({'pod_uid':'original-pod','container_id':'original-container','attached_monotonic':stream.started,'stream':stream})
            stream.child.wait(timeout=2)
            self.assertIsNone(pool.witness('different-pod','original-container'))
            self.assertIsNone(pool.witness('original-pod','different-container'))
            result=pool.witness('original-pod','original-container')
            if code:self.assertIsNone(result)
            else:self.assertEqual(result['container_id'],'original-container')
        self.assertTrue(all(v['original_process_removed'] for v in pool.close()))

    def test_private_output_is_never_returned_as_a_witness(self):
        from test_kyverno_log_witness_fixture import BoundedLogStream
        secret=b'private-witness-value'
        stream=BoundedLogStream([sys.executable,'-u','-c',"print('private-witness-value',flush=True)"],[secret],seconds=2)
        stream.child.wait(timeout=2)
        with self.assertRaisesRegex(ValueError,'Private credential'):stream.snapshot()
        with self.assertRaisesRegex(ValueError,'Private credential'):stream.close()
        self.assertIsNotNone(stream.child.returncode)


class PreviousLogDiagnosticContracts(unittest.TestCase):
    def test_failed_task_diagnostics_preserve_primary_error_and_suppress_private_details(self):
        import contextlib
        import io
        import subprocess
        from unittest import mock

        from test_gerrit_worker_runtime_fixture import failure_tasks
        primary=RuntimeError('original failure')
        output=io.StringIO();manager=mock.Mock();manager.call.side_effect=subprocess.TimeoutExpired(['private-command'],1)
        with contextlib.redirect_stdout(output),self.assertRaises(RuntimeError) as caught:
            try:raise primary
            except RuntimeError:
                self.assertEqual(failure_tasks(manager,'fixture'),[])
                raise
        self.assertIs(caught.exception,primary);self.assertNotIn('private-command',output.getvalue())
        self.assertIn('TimeoutExpired',output.getvalue());manager.call.assert_called_once()

    def test_successful_previous_api_read_is_authoritative_over_truncated_follow(self):
        from test_kyverno_log_witness_fixture import select_previous_logs
        api={'returncode':0,'stdout':'leadership lost, stopped leading','stderr':''}
        stream={'returncode':0,'capture_error':None,'stdout':'startup log only'}
        selected=select_previous_logs(api,stream)
        self.assertEqual(selected,{'previous_logs':api['stdout'],'previous_logs_source':'previous-container-api'})
        # Do not cherry-pick a favorable follow record over a successful API
        # capture that fails the strict fencing predicate.
        api['stdout']='no shutdown evidence';stream['stdout']='leadership lost, stopped leading'
        self.assertEqual(select_previous_logs(api,stream)['previous_logs'],api['stdout'])

    def test_original_follow_is_only_a_fallback_for_unavailable_previous_api(self):
        from test_kyverno_log_witness_fixture import select_previous_logs
        stream={'returncode':0,'capture_error':None,'stdout':'leadership lost, stopped leading'}
        for api in (None,{'returncode':1,'stdout':''}):
            self.assertEqual(select_previous_logs(api,stream)['previous_logs_source'],'original-follow-stream')
            for changed in ({**stream,'returncode':1},{**stream,'capture_error':'capture_deadline'},None):
                self.assertEqual(select_previous_logs(api,changed),{})

    def test_failed_guest_journal_diagnostic_preserves_remaining_reads(self):
        import subprocess
        from types import SimpleNamespace
        from unittest import mock

        from test_k3s_vm_fixture import guest_failure_journals
        nodes=[{'name':'first'},{'name':'second'}]
        ssh=mock.Mock(side_effect=[subprocess.TimeoutExpired(['private-argument'],10),SimpleNamespace(stdout='healthy private-value',stderr='')])
        result=guest_failure_journals(nodes,ssh,['private-value'])
        self.assertEqual(result['first'],{'capture_failed':True,'exception_type':'TimeoutExpired'})
        self.assertEqual(result['second'],'healthy <redacted>');self.assertNotIn('private-argument',json.dumps(result))
        self.assertEqual(ssh.call_count,2)

    def test_previous_log_errors_remain_bounded_private_diagnostics(self):
        from types import SimpleNamespace
        from unittest import mock

        import test_kyverno_log_witness_fixture as logs
        client=SimpleNamespace(prefix=['kubectl']);pod={'metadata':{'name':'original-pod','namespace':'kyverno'}}
        error=SimpleNamespace(returncode=1,stdout='',stderr='previous container log unavailable')
        with mock.patch.object(logs,'run_bounded',return_value=error) as run:
            result=logs.previous_logs(client,pod,'controller',[])
        self.assertEqual(result,{'returncode':1,'stdout':'','stderr':'previous container log unavailable'})
        self.assertIn('--previous',run.call_args.args[0]);self.assertEqual(run.call_args.kwargs['output_max_bytes'],1024*1024)
        with mock.patch.object(logs,'run_bounded',return_value=error),self.assertRaisesRegex(ValueError,'Private credential'):
            logs.previous_logs(client,pod,'controller',[b'previous container'])


if __name__=='__main__':unittest.main()
