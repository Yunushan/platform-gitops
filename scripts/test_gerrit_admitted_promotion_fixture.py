#!/usr/bin/env python3
"""Original Gerrit CI, eight signed bindings and Argo rollout under real admission."""

import copy
import json
import os
import ssl
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import HTTPSHandler, ProxyHandler, build_opener

from bounded_subprocess import run_bounded
from ci_gitops_handoff import argocd_observed
from strict_json import loads_strict_json
from test_gerrit_artifact_promotion_worker_fixture import (
    NAME,
    NAMESPACE,
    deployment,
    retain,
)
from test_gerrit_signed_promotion_fixture import SignedPromotionChecks
from test_kyverno_admission_authority_fixture import (
    AuthorityChecks,
    unsigned_init_container,
)
from test_kyverno_private_tuf_fixture import ORIGIN, PrivateTUFChecks


class AdmittedPromotionChecks(AuthorityChecks, SignedPromotionChecks, PrivateTUFChecks):
    admission_workers = True
    application_replicas = 3
    admission_failures = False

    def worker_workload(self,test,context,native):
        self.ci_admission_context=context
        with ExitStack() as lifetime:
            self.private_tuf_lifetime=lifetime
            try:return super().worker_workload(test,context,native)
            except Exception:
                if hasattr(self, 'ci_admission'):
                    try:
                        self.retain_promotion_failure(context)
                    except (OSError, ValueError, TimeoutError) as error:
                        print(json.dumps({'admission_failure_capture_failed':type(error).__name__}),flush=True)
                if self.native_watch and hasattr(self,'admission_watch_progress'):
                    try:
                        pair=getattr(self,'admission_watch_pair',None)
                        self.admission_watch_progress['fresh_poll_observations']=copy.deepcopy(getattr(pair,'fresh_poll_observations',{}))
                        retain('native-watch-admission-progress.json',self.admission_watch_progress)
                    except (OSError,ValueError) as error:
                        print(json.dumps({'native_watch_progress_capture_failed':type(error).__name__}),flush=True)
                raise
            finally:del self.private_tuf_lifetime

    def retain_promotion_failure(self, context):
        client = context['client']
        app = client.get('application.argoproj.io/application', 'argocd', optional=True) or {}
        status = app.get('status', {})
        events = loads_strict_json(client.call(['get', 'events', '-n', NAMESPACE, '-o', 'json'], seconds=15))['items']
        keywords = ('denied the request', 'failed calling webhook', 'failed to verify', 'x509',
                    'connection refused', 'deadline exceeded', 'insufficient memory', 'insufficient cpu')
        warnings = []
        for event in events:
            if event.get('type') != 'Warning':
                continue
            message = event.get('message', '').lower()
            warnings.append({'kind': event.get('involvedObject', {}).get('kind'),
                             'reason': event.get('reason'), 'signals': [word for word in keywords if word in message]})
        retain('admitted-promotion-failure.json', {
            'runtime': context.get('runtime', 'k3s'), 'pre_push_negative_cases': self.admission_negatives,
            'application_health': status.get('health', {}).get('status'),
            'application_sync': status.get('sync', {}).get('status'),
            'operation_phase': status.get('operationState', {}).get('phase'), 'warnings': warnings,
        })

    def promote_artifact(self,test,context,gerrit,source,run,receipt,execution,registry):
        self.admission_watch_pair=execution.get('watch_pair')
        test.assertEqual(self.admission_watch_pair is not None,self.native_watch)
        return super().promote_artifact(test,context,gerrit,source,run,receipt,execution,registry)

    def prepare_signed_promotion(self,test,plan,report,contract,template,arguments):
        self.admission_live_ci_arguments=arguments
        return super().prepare_signed_promotion(test,plan,report,contract,template,arguments)

    def mirror_alive(self,native):
        original=self.private_tuf_active_process
        proc=Path('/proc')/str(original['pid'])
        test=native['test']
        test.assertEqual(int((proc/'stat').read_text().rsplit(') ',1)[1].split()[19]),original['start_ticks'])
        test.assertEqual((proc/'cgroup').read_text().strip(),original['cgroup'])
        test.assertEqual(Path('/proc/sys/kernel/random/boot_id').read_text().strip(),original['boot_id'])
        opener=build_opener(ProxyHandler({}),HTTPSHandler(context=ssl.create_default_context(cafile=str(native['root']/'ca.crt'))))
        with opener.open(ORIGIN+'/root.json',timeout=5) as response:
            test.assertEqual(response.read(),self.admission_tuf_files['root.json'].encode())

    def dryrun_pod(self,namespace,name,image):
        spec=copy.deepcopy(deployment(image,replicas=self.application_replicas)['spec']['template']['spec'])
        return {'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':namespace},'spec':spec}

    def refuse(self,test,document,policy,case,arguments):
        client=self.ci_admission_context['client']
        result=run_bounded([*client.prefix,'--request-timeout=45s',*arguments],input=json.dumps(document),text=True,timeout=60,check=False,output_max_bytes=1024*1024)
        text=result.stderr
        test.assertFalse(any(v in text.encode() for v in self.signature_private_values))
        if not (result.returncode and 'denied the request' in text and policy in text and 'Release signature and signed bindings must verify.' in text):
            target=Path(os.environ['KYVERNO_ADMISSION_TEST_REPORT']).parent/('joined-denial-'+case+'.log')
            target.write_text(text,encoding='utf-8');target.chmod(0o600)
            raise AssertionError('The original named release policy did not refuse the negative request')
        self.admission_negatives.append({'case':case,'named_signature_policy_denial':True,'server_dry_run':True})

    def private_tuf_live(self,native,files,status,policy):
        context=self.ci_admission_context;test=native['test'];client=context['client']
        self.admission_tuf_files=files;self.mirror_alive(native)
        test.assertEqual(len(self.signature_plan['annotations']),8)
        test.assertEqual(policy['spec']['attestors'][0]['cosign']['annotations'],self.signature_plan['annotations'])
        selected,installed,digest=self.install_authority(native,context)
        namespaces={NAMESPACE:client.get('namespace/'+NAMESPACE)['metadata']['uid']}
        wrong_bindings={'ci-wrong-binding-'+str(i):key for i,key in enumerate(self.signature_plan['annotations'])}
        for name in ('ci-wrong-key',*wrong_bindings):
            obj=loads_strict_json(client.call(['create','-f','-','-o','json'],payload=json.dumps({'apiVersion':'v1','kind':'Namespace','metadata':{'name':name,'labels':{'pod-security.kubernetes.io/enforce':'restricted'}}}).encode()))
            namespaces[name]=obj['metadata']['uid']
        policies,webhooks=self.compile_authority_policies(native,context,policy,namespaces,wrong_bindings=wrong_bindings)
        image=self.signature_plan['image']
        original=client.get('deployment/'+NAME,NAMESPACE)
        unsigned=original['spec']['template']['spec']['containers'][0]['image'];test.assertNotEqual(unsigned,image)
        before_pods=loads_strict_json(client.call(['get','pods','-n',NAMESPACE,'-o','json']))['items']
        test.assertFalse(any(c['image']==image for p in before_pods for c in p['spec']['containers']))
        self.admission_negatives=[]
        for namespace,selected_image,case in ((NAMESPACE,unsigned,'unsigned'),('ci-wrong-key',image,'wrong-key'),*((name,image,name) for name in wrong_bindings)):
            self.refuse(test,self.dryrun_pod(namespace,'deny-'+case,selected_image),namespace,case,['create','--dry-run=server','-f','-'])
        document=self.dryrun_pod(NAMESPACE,'deny-init',image)
        document['spec']['initContainers']=[unsigned_init_container(document['spec']['containers'][0],unsigned)]
        self.refuse(test,document,NAMESPACE,'unsigned-init',['create','--dry-run=server','-f','-'])
        client.call(['create','--dry-run=server','-f','-'],payload=json.dumps(self.dryrun_pod(NAMESPACE,'signed-positive',image)).encode())
        authority=self.audit_authority(native,context,installed,namespaces,policies)
        self.ci_admission={'plan':selected,'installed':installed,'package_contract_sha256':digest,'policies':policies,'namespace_uids':namespaces,
            'webhooks_before_promotion':webhooks,'before':authority,'original_deployment':original,
            'original_application':client.get('application.argoproj.io/application','argocd'),
            'unsigned_image':unsigned,'image':image,'annotations':copy.deepcopy(self.signature_plan['annotations']),
            'policy_ready_before_git_push_verified':True,'worker_pause_or_failure_exercised':False,'full_stack_verified':False,'physical_ha_verified':False}

    def verify_promoted_admission(self,test,context,actual,live,pods,application):
        state=self.ci_admission;native=self.signature_context;client=context['client']
        self.mirror_alive(native)
        test.assertEqual(state['image'],self.signed_ci_binding['evidence']['artifact']['image'])
        test.assertEqual(state['annotations'],self.signed_ci_binding['annotations'])
        test.assertEqual(live['metadata']['uid'],state['original_deployment']['metadata']['uid'])
        test.assertEqual(live['spec']['replicas'],self.application_replicas)
        test.assertEqual(len(pods),self.application_replicas)
        test.assertEqual(application['metadata']['uid'],state['original_application']['metadata']['uid'])
        test.assertEqual(application['spec'],state['original_application']['spec'])
        test.assertEqual(application['status']['health']['status'],'Healthy')
        test.assertEqual(application['status']['sync']['status'],'Synced')
        after=self.audit_authority(native,context,state['installed'],state['namespace_uids'],state['policies'])
        for name,original in state['before']['controllers'].items():
            test.assertEqual(after['controllers'][name]['metadata']['uid'],original['metadata']['uid'])
            test.assertEqual(after['controllers'][name]['spec'],original['spec'])
        originals={p['metadata']['uid']:p for p in state['before']['controller_pods']}
        test.assertEqual({p['metadata']['uid'] for p in after['controller_pods']},set(originals))
        for pod in after['controller_pods']:
            original=originals[pod['metadata']['uid']];test.assertEqual(pod['spec'],original['spec'])
            test.assertEqual([(v['containerID'],v['restartCount']) for v in pod['status']['containerStatuses']],[(v['containerID'],v['restartCount']) for v in original['status']['containerStatuses']])
        test.assertEqual({p['metadata']['uid']:p['spec'] for p in after['controller_disruption_budgets']},{p['metadata']['uid']:p['spec'] for p in state['before']['controller_disruption_budgets']})
        ephemeral={'name':'unsigned-debug','targetContainerName':'web','image':state['unsigned_image'],
            'command':['/bin/sh','-ec','sleep 60'],'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']},'runAsNonRoot':True,'runAsUser':1000}}
        self.refuse_unsigned_ephemeral(test,pods[0],ephemeral)
        test.assertFalse(client.get('pod/'+pods[0]['metadata']['name'],NAMESPACE)['spec'].get('ephemeralContainers'))
        for pod in pods:
            test.assertEqual(pod['spec']['containers'][0]['image'],state['image'])
            test.assertTrue(pod['status']['containerStatuses'][0]['imageID'].endswith('@'+state['image'].split('@')[1]))
        state.update(after=after,negative_cases=self.admission_negatives,application=application,workload=live,pods=pods,
            actual_merged_ci_to_admitted_argocd_rollout_verified=True,actual_admission_verified=True,
            all_eight_signed_ci_bindings_enforced=True,original_authorities_preserved=True)
        failures=self.verify_original_workload_failures(test,context,actual,live,pods,application) if self.admission_failures else {}
        return {'actual_merged_ci_to_admitted_argocd_rollout_verified':True,'signed_image_admission_verified':True,'all_eight_signed_ci_bindings_enforced':True,**failures}

    def verify_original_workload_failures(self,test,context,actual,live,pods,application):
        from test_harbor_worker_fixture import http_ready
        from test_kyverno_failure_fixture import exercise as failures
        state=self.ci_admission;client=context['client'];native=self.signature_context
        service=client.get('service/'+NAME,NAMESPACE)
        test.assertEqual(service['metadata']['uid'],actual['service']['metadata']['uid'])
        baseline={**state['after'],'webhooks':loads_strict_json(client.call(['get','validatingwebhookconfigurations','-o','json']))['items'],
            'workload':copy.deepcopy(live),'service':service,'pods':copy.deepcopy(pods)}
        signed=self.dryrun_pod(NAMESPACE,'signed-failure-probe',state['image'])
        unsigned=self.dryrun_pod(NAMESPACE,'unsigned-failure-probe',state['unsigned_image'])
        port=service['spec']['ports'][0]['nodePort']
        serving=lambda node:http_ready(test,node['address'],port)
        watches=self.admission_watch_pair
        watch_before=watches.snapshot() if watches is not None else None
        if watches is not None:test.assertEqual(set(watch_before['watches']),{'bridge','reporter'})
        if watches is not None:self.admission_watch_progress={'stage':'before-faults','before':watch_before}
        proof=failures(test,context,baseline,state['policies'],signed,unsigned,serving,self.signature_private_values)
        if watches is not None:self.admission_watch_progress.update(stage='worker-faults-recovered')
        self.mirror_alive(native)
        after=self.audit_authority(native,context,state['installed'],state['namespace_uids'],state['policies'])
        original=state['original_application'];app_resource='application.argoproj.io/'+original['metadata']['name'];app_namespace=original['metadata']['namespace']
        fence=datetime.now(UTC)
        # Argo emits whole-second timestamps; cross the fence before requesting refresh.
        context['wait'](lambda:datetime.now(UTC).replace(microsecond=0)>fence,seconds=5)
        before_refresh=client.get(app_resource,app_namespace)
        test.assertEqual(before_refresh['metadata']['uid'],original['metadata']['uid']);test.assertEqual(before_refresh['spec'],original['spec'])
        refresh=[{'op':'test','path':'/metadata/uid','value':original['metadata']['uid']},
            {'op':'test','path':'/metadata/resourceVersion','value':before_refresh['metadata']['resourceVersion']},
            {'op':'add','path':'/metadata/annotations','value':{**before_refresh['metadata'].get('annotations',{}),'argocd.argoproj.io/refresh':'hard'}}]
        requested=loads_strict_json(client.call(['patch',app_resource,'-n',app_namespace,'--type=json','-p',json.dumps(refresh),'-o','json']))
        test.assertEqual(requested['metadata']['uid'],original['metadata']['uid']);test.assertEqual(requested['spec'],original['spec'])
        recovery={'reconciliation_fence_at':fence.isoformat(),'refresh_before':before_refresh,'refresh_requested':requested}
        def recovered_application():
            current=client.get(app_resource,app_namespace)
            test.assertEqual(current['metadata']['uid'],state['original_application']['metadata']['uid'])
            test.assertEqual(current['spec'],state['original_application']['spec'])
            recovery['application']=current
            return argocd_observed(current,actual['result']['promoted_revision'],since=fence)
        recovery['completed']=False
        try:
            context['wait'](recovered_application,seconds=90)
            recovery['completed']=True
        finally:
            # Preserve the exact last Application even when freshness fails;
            # completed-command proofs must not hide a failed final observation.
            try:retain('post-fault-argocd-observation.json',recovery)
            except (OSError,ValueError) as error:
                print(json.dumps({'post_fault_argo_diagnostic_capture_failed':type(error).__name__}),flush=True)
        recovered_live=client.get('deployment/'+NAME,NAMESPACE);recovered_service=client.get('service/'+NAME,NAMESPACE)
        test.assertEqual(recovered_live['metadata']['uid'],live['metadata']['uid']);test.assertEqual(recovered_live['spec'],live['spec'])
        test.assertEqual(recovered_live['status']['observedGeneration'],recovered_live['metadata']['generation'])
        for field in ('replicas','readyReplicas','updatedReplicas','availableReplicas'):test.assertEqual(recovered_live['status'].get(field),3)
        test.assertEqual(recovered_service['metadata']['uid'],service['metadata']['uid']);test.assertEqual(recovered_service['spec'],service['spec'])
        current_pods=loads_strict_json(client.call(['get','pods','-n',NAMESPACE,'-l','app='+NAME,'-o','json']))['items']
        originals={p['metadata']['uid']:p for p in pods}
        test.assertEqual({p['metadata']['uid'] for p in current_pods},set(originals))
        for pod in current_pods:
            original=originals[pod['metadata']['uid']];test.assertEqual(pod['spec'],original['spec'])
            test.assertTrue(any(c['type']=='Ready' and c['status']=='True' for c in pod['status']['conditions']))
            statuses=pod['status']['containerStatuses'];test.assertEqual(len(statuses),len(pod['spec']['containers']))
            test.assertTrue(all(c['ready'] is True and 'running' in c.get('state',{}) for c in statuses))
            test.assertEqual([(c['containerID'],c['restartCount']) for c in statuses],[(c['containerID'],c['restartCount']) for c in original['status']['containerStatuses']])
            test.assertTrue(statuses[0]['imageID'].endswith('@'+state['image'].split('@')[1]))
        for node in context['nodes'][1:]:context['wait'](lambda node=node:serving(node),seconds=30)
        watch_result={}
        if watches is not None:
            import gerrit_artifact_promotion as promotion
            self.admission_watch_progress.update(stage='fresh-post-fault-polls')
            fresh_polls={kind:watches.fresh_poll(kind) for kind in ('bridge','reporter')}
            args=self.admission_live_ci_arguments
            data,evidence=promotion.legacy.verified_ci(args[4],args[6],args[1],args[2],args[3],self.signed_ci_binding['promotion_plan'],args[7])
            test.assertEqual((data,evidence),(self.signed_ci_binding['data'],self.signed_ci_binding['evidence']))
            watch_after=watches.snapshot()
            self.admission_watch_progress.update(stage='fresh-original-ci-verified',after=watch_after,fresh_polls=fresh_polls)
            for kind in ('bridge','reporter'):
                test.assertEqual(watch_after['watches'][kind]['process'],watch_before['watches'][kind]['process'])
                test.assertEqual(watch_after['watches'][kind]['unit'],watch_before['watches'][kind]['unit'])
                test.assertEqual(watch_after['watches'][kind]['invocation_id'],watch_before['watches'][kind]['invocation_id'])
                test.assertEqual(watch_after['watches'][kind]['restart_count'],watch_before['watches'][kind]['restart_count'])
                test.assertEqual(watch_after['watches'][kind]['log_identity'],watch_before['watches'][kind]['log_identity'])
            state['native_watch_continuity']={'before':watch_before,'after':watch_after,'fresh_polls':fresh_polls,'live_ci_after_faults':{'data':data,'evidence':evidence},
                'same_original_restarted_watchers_verified':True,'fresh_polling_after_faults_verified':True}
            watch_result={'native_watch_polling_after_admission_faults_verified':True,'native_watch_original_identity_after_admission_faults_verified':True}
        # Update the caller's original evidence objects so retained rollout proofs
        # represent the final recovered Application/Deployment/Pods, not stale health.
        application.clear();application.update(recovery['application'])
        live.clear();live.update(recovered_live);pods[:]=current_pods
        state.update(application=application,workload=live,pods=pods,worker_pause_or_failure_exercised=True,
            failure={'baseline':baseline,'proof':proof,'after_recovery':{'authority':after,'application':application,'workload':live,'pods':pods,'service':recovered_service,'reconciliation':recovery}},
            original_argo_signed_workload_recovery_verified=True,fresh_argo_reconciliation_after_faults_verified=True)
        return {'original_ci_admission_worker_failure_recovery_verified':True,'original_ci_all_admission_backends_down_fail_closed_verified':True,
            'original_ci_admission_disruption_budget_enforced':True,'original_argo_signed_workload_recovery_verified':True,'fresh_argo_reconciliation_after_faults_verified':True,**watch_result}

    def exercise(self,context):
        result=super().exercise(context)
        context['test'].assertTrue(self.ci_admission['actual_merged_ci_to_admitted_argocd_rollout_verified'])
        if self.admission_failures:
            context['test'].assertTrue(self.ci_admission['worker_pause_or_failure_exercised'])
            context['test'].assertTrue(self.ci_admission['original_argo_signed_workload_recovery_verified'])
        context['test'].assertEqual(len(self.tuf_processes),3)
        context['test'].assertTrue(all(p['original_process_removed'] for p in self.tuf_processes))
        sha=retain('admitted-ci-promotion.json',self.ci_admission)
        result.update(valkey_gateway_harbor_signed_image_admission_verified=True,
            valkey_gateway_harbor_merged_ci_admitted_promotion_proof_sha256={'proof':sha})
        return result


def exercise(test,*,admission_failures=False,native_watch=False):
    if type(admission_failures) is not bool or type(native_watch) is not bool:raise ValueError("Select explicit boolean original-CI admission and native-watch profiles")
    if native_watch and not admission_failures:raise ValueError("Joined native watches require the explicit admission failure profile")
    if admission_failures and not os.environ.get("K3S_VM_TEST_TIME_ARTIFACTS","").strip():raise ValueError("Original-CI worker pause tests require the pinned guest time service")
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    from test_harbor_worker_fixture import (
        CoreReplicaChecks,
        DatabasePromotionChecks,
        JobReplicaChecks,
        JWTChecks,
        PoolHealthChecks,
        RegistryChecks,
        ServiceChecks,
    )
    class Application(AdmittedPromotionChecks,PoolHealthChecks,JobReplicaChecks,CoreReplicaChecks,JWTChecks,DatabasePromotionChecks,ServiceChecks,RegistryChecks,gateway.Application):pass
    Application.admission_failures=admission_failures
    Application.native_watch=native_watch
    Application.guest_runtime_seconds=1700 if admission_failures else 1100
    return cache.exercise(test,application=Application())
