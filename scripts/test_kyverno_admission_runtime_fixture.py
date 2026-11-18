#!/usr/bin/env python3
"""Actual three-node Kyverno admission and signed private-image workload execution."""

import base64
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path

import publish_harbor_static_image as publisher
from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from kubernetes_management import KubernetesOperationError
from strict_json import loads_strict_json
from test_harbor_worker_fixture import aggregate_worker_report, base_image
from test_k3s_vm_fixture import PROBE_MANIFEST
from test_kyverno_admission_authority_fixture import AuthorityChecks
from test_kyverno_private_tuf_fixture import PrivateTUFChecks


class AdmissionChecks(AuthorityChecks, PrivateTUFChecks):
    def signature_setup(self,context,**kwargs):
        core=self.service_clients['harbor-core'];ca=(context['root']/'ca.crt').read_bytes()
        plan={'origin':'https://192.0.2.91:8443','repository':'fixture/ha','leaf_sha256':core.leaf}
        registry=publisher.Registry(plan,ca,self.robot_credential);registry.authenticate()
        blobs=base_image();manifest=blobs.pop('blobs/sha256/'+PROBE_MANIFEST)
        for raw in blobs.values():registry.upload(raw)
        code,headers,_=registry.request('PUT',registry.prefix+'/manifests/sha256:'+PROBE_MANIFEST,manifest,publisher.MEDIA)
        context['test'].assertEqual(code,201);context['test'].assertEqual(headers['docker-content-digest'],'sha256:'+PROBE_MANIFEST)
        self.admission_image='192.0.2.91:8443/fixture/ha@sha256:'+PROBE_MANIFEST
        super().signature_setup(context,image=self.admission_image,**kwargs)

    def private_tuf_live(self,native,files,status,policy):
        import test_k3s_vm_fixture as vm
        self.admission_report=vm.exercise(native['test'],admission_workers=True,
            workload=lambda context:self.admission_workload(native,context,policy))

    def admission_workload(self,native,context,policy):
        test=native['test'];client=context['client'];stage='import-images'
        def create(obj):return loads_strict_json(client.call(['create','-f','-','-o','json'],payload=json.dumps(obj).encode()))
        try:
            selected,installed,digest=self.install_authority(native,context)
            namespaces={}
            for name in ('admission-fixture','admission-wrong-key','admission-wrong-binding'):
                namespaces[name]=create({'apiVersion':'v1','kind':'Namespace','metadata':{'name':name,'labels':{'pod-security.kubernetes.io/enforce':'restricted'}}})['metadata']['uid']
            stage='compile-original-namespaced-signature-policies'
            print(json.dumps({'kyverno_admission_stage':stage}),flush=True)
            policies,webhook_rows=self.compile_authority_policies(native,context,policy,namespaces)
            stage='actual-api-negative-and-positive-admission'
            print(json.dumps({'kyverno_admission_stage':stage}),flush=True)
            def pod(namespace,name,image):
                return {'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':namespace},'spec':{'restartPolicy':'Never','automountServiceAccountToken':False,
                    'securityContext':{'runAsNonRoot':True,'runAsUser':1000,'runAsGroup':1000,'seccompProfile':{'type':'RuntimeDefault'}},
                    'containers':[{'name':'app','image':image,'command':['/bin/sh','-ec','sleep 60'],'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},
                        'resources':{'requests':{'cpu':'10m','memory':'16Mi'},'limits':{'cpu':'500m','memory':'64Mi'}}}]}}
            negatives=[]
            def refused(document,name,mode,arguments):
                result=run_bounded([*client.prefix,'--request-timeout=45s',*arguments],input=json.dumps(document),text=True,timeout=60,check=False,output_max_bytes=1024*1024)
                test.assertNotEqual(result.returncode,0)
                # Timeouts, PodSecurity refusals and network failures cannot prove enforcement.
                text=result.stderr
                if not ('denied the request' in text and name in text and 'Release signature and signed bindings must verify.' in text):
                    private=Path(os.environ['KYVERNO_ADMISSION_TEST_REPORT']).parent/'admission-denial-at-failure.log'
                    private.write_text(text);private.chmod(0o600)
                    raise AssertionError('Expected named signature-policy denial was not returned')
                test.assertFalse(any(value in text.encode() for value in self.signature_private_values))
                negatives.append({'case':mode,'refused':True,'named_signature_policy_denial':True})
            unsigned='192.0.2.91:8443/fixture/ha@'+publisher.digest(self.robot_witness[2][2])
            for name,image,mode in (('admission-fixture',unsigned,'unsigned'),('admission-wrong-key',self.admission_image,'wrong-key'),('admission-wrong-binding',self.admission_image,'wrong-binding'),('admission-fixture',unsigned,'unsigned-init')):
                document=pod(name,'deny-'+mode,self.admission_image if mode=='unsigned-init' else image)
                if mode=='unsigned-init':document['spec']['initContainers']=[{**document['spec']['containers'][0],'name':'init','image':unsigned}]
                refused(document,name,mode,['create','--dry-run=server','-f','-'])
            client.call(['create','--dry-run=server','-f','-'],payload=json.dumps(pod('admission-fixture','positive',self.admission_image)).encode())
            auth=base64.b64encode(('fixture-reader:'+self.reader_password).encode()).decode()
            config={'auths':{'192.0.2.91:8443':{'auth':auth}}}
            create({'apiVersion':'v1','kind':'Secret','metadata':{'name':'application-pull','namespace':'admission-fixture'},'immutable':True,'type':'kubernetes.io/dockerconfigjson','data':{'.dockerconfigjson':base64.b64encode(json.dumps(config).encode()).decode()}})
            template=pod('admission-fixture','signed-web',self.admission_image)['spec'];template.pop('restartPolicy')
            template.update(imagePullSecrets=[{'name':'application-pull'}],affinity={'podAntiAffinity':{'requiredDuringSchedulingIgnoredDuringExecution':[{'labelSelector':{'matchLabels':{'app':'signed-web'}},'topologyKey':'kubernetes.io/hostname'}]}},volumes=[{'name':'scratch','emptyDir':{}}])
            container=template['containers'][0];container.update(command=['/bin/sh','-ec','mkdir -p /tmp/www; printf signed-admitted > /tmp/www/index.html; exec /bin/httpd -f -p 8080 -h /tmp/www'],volumeMounts=[{'name':'scratch','mountPath':'/tmp'}],ports=[{'name':'http','containerPort':8080}],readinessProbe={'httpGet':{'path':'/','port':8080},'periodSeconds':2})
            template['securityContext']['fsGroup']=1000
            workload=create({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'signed-web','namespace':'admission-fixture'},'spec':{'replicas':3,'strategy':{'type':'RollingUpdate','rollingUpdate':{'maxSurge':0,'maxUnavailable':1}},'selector':{'matchLabels':{'app':'signed-web'}},'template':{'metadata':{'labels':{'app':'signed-web'}},'spec':template}}})
            service=create({'apiVersion':'v1','kind':'Service','metadata':{'name':'signed-web','namespace':'admission-fixture'},'spec':{'type':'NodePort','selector':{'app':'signed-web'},'ports':[{'port':8080,'targetPort':'http'}]}})
            def workload_ready():
                obj=client.get('deployment.apps/signed-web','admission-fixture');test.assertEqual(obj['metadata']['uid'],workload['metadata']['uid'])
                return obj.get('status',{}).get('readyReplicas')==3 and obj['status'].get('updatedReplicas')==3 and obj['status'].get('availableReplicas')==3
            context['wait'](workload_ready,seconds=180)
            pods=loads_strict_json(client.call(['get','pods','-n','admission-fixture','-o','json']))['items'];test.assertEqual(len(pods),3)
            test.assertEqual({p['spec']['nodeName'] for p in pods},{n['name'] for n in context['nodes'][1:]})
            for item in pods:
                test.assertTrue(item['status']['containerStatuses'][0]['ready']);test.assertTrue(item['status']['containerStatuses'][0]['imageID'].endswith('@sha256:'+PROBE_MANIFEST))
            ephemeral=copy.deepcopy(pods[0]);ephemeral['spec']['ephemeralContainers']=[{
                'name':'unsigned-debug','targetContainerName':'app','image':unsigned,'command':['/bin/sh','-ec','sleep 60'],
                'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']},'runAsNonRoot':True,'runAsUser':1000}}]
            refused(ephemeral,'admission-fixture','unsigned-ephemeral',['replace','--raw','/api/v1/namespaces/admission-fixture/pods/'+pods[0]['metadata']['name']+'/ephemeralcontainers?dryRun=All','-f','-'])
            test.assertFalse(client.get('pod/'+pods[0]['metadata']['name'],'admission-fixture')['spec'].get('ephemeralContainers'))
            def serving(node):
                connection=http.client.HTTPConnection(node['address'],service['spec']['ports'][0]['nodePort'],timeout=5)
                try:
                    connection.request('GET','/');response=connection.getresponse()
                    return response.status==200 and response.read(1024)==b'signed-admitted'
                finally:connection.close()
            for node in context['nodes'][1:]:context['wait'](lambda node=node:serving(node),seconds=30)
            stage='audit-original-live-controller-profile'
            authority=self.audit_authority(native,context,installed,namespaces,policies)
            proof={**authority,'plan':selected,'package_contract_sha256':digest,'webhooks':webhook_rows,
                'negative_cases':negatives,'workload':client.get('deployment.apps/signed-web','admission-fixture'),'pods':pods,'service':service,
                'signed_image':self.admission_image,'actual_admission_verified':True,'three_serving_nodes_verified':True,'physical_ha_verified':False,'full_stack_verified':False}
            if self.admission_failures:
                from test_kyverno_failure_fixture import exercise as failures
                proof['admission_failure']=failures(test,context,proof,policies,pod('admission-fixture','signed-failure-probe',self.admission_image),
                    pod('admission-fixture','unsigned-failure-probe',unsigned),serving,self.signature_private_values)
            self.admission_proof=proof
            result={'kyverno_real_signed_image_admission_verified':True,'kyverno_three_admission_replicas_verified':True,'kyverno_original_controller_resources_preserved':True,
                'kyverno_negative_signature_and_init_images_refused':True,'kyverno_unsigned_ephemeral_image_refused':True,'kyverno_actual_signed_image_workload_on_three_workers_verified':True,'kyverno_ha_verified':False,'full_stack_verified':False}
            if self.admission_failures:result.update(kyverno_admission_worker_failure_recovery_verified=True,kyverno_all_admission_backends_down_fail_closed_verified=True,kyverno_admission_disruption_budget_enforced=True)
            return result
        except Exception as error:
            try:
                private=Path(os.environ['KYVERNO_ADMISSION_TEST_REPORT']).parent/'policy-status-at-failure.json'
                raw=client.call(['get','imagevalidatingpolicies','-o','json']);private.write_text(raw);private.chmod(0o600)
                print(json.dumps({'policy_ready_at_failure':{v['metadata']['name']:v.get('status',{}).get('conditionStatus',{}).get('ready') for v in loads_strict_json(raw)['items']}}),flush=True)
            except (OSError,ValueError,TypeError,KeyError):print(json.dumps({'policy_diagnostic_capture_failed':True}),flush=True)
            if isinstance(error,KubernetesOperationError):print(json.dumps({'kyverno_operation_failure':error.diagnostic,'stage':stage}),flush=True)
            try:
                rows=loads_strict_json(client.call(['get','pods','-A','-o','json']))['items']
                print(json.dumps({'kyverno_failure_stage':stage,'pods':[{'namespace':p['metadata']['namespace'],'name':p['metadata']['name'],'phase':p.get('status',{}).get('phase'),'conditions':[{k:c.get(k) for k in ('type','status','reason')} for c in p.get('status',{}).get('conditions',[])]} for p in rows]}),flush=True)
            except (OSError,ValueError,TypeError,KeyError):print(json.dumps({'diagnostic_capture_failed':True}),flush=True)
            raise

    def exercise(self,context):
        result=super().exercise(context)
        report=self.admission_report
        result.update(aggregate_worker_report(report,read_bounded_bytes(Path(os.environ['K3S_VM_TEST_REPORT']),max_bytes=2*1024*1024)))
        target=Path(os.environ['KYVERNO_ADMISSION_TEST_REPORT']);descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w') as stream:json.dump(self.admission_proof,stream,sort_keys=True);stream.write('\n')
        result['valkey_gateway_harbor_kyverno_admission_proof_sha256']={'proof':hashlib.sha256(target.read_bytes()).hexdigest()}
        return result


def exercise(test,*,admission_failures=False):
    if type(admission_failures) is not bool:raise ValueError("Select the explicit admission failure fixture")
    if admission_failures and not os.environ.get("K3S_VM_TEST_TIME_ARTIFACTS","").strip():raise ValueError("Admission worker pause tests require the pinned guest time service")
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    from test_harbor_core_replica_fixture import CoreReplicaChecks
    from test_harbor_database_promotion_fixture import DatabasePromotionChecks
    from test_harbor_jobservice_replica_fixture import JobReplicaChecks
    from test_harbor_jwt_runtime_fixture import JWTChecks
    from test_harbor_pool_health_fixture import PoolHealthChecks
    from test_harbor_registry_runtime_fixture import RegistryChecks
    from test_harbor_service_runtime_fixture import ServiceChecks
    class Application(AdmissionChecks,PoolHealthChecks,JobReplicaChecks,CoreReplicaChecks,JWTChecks,DatabasePromotionChecks,ServiceChecks,RegistryChecks,gateway.Application):pass
    Application.admission_failures=admission_failures
    return cache.exercise(test,application=Application())
