#!/usr/bin/env python3
"""Opt-in actual scheduler/merger/web pairs with real PostgreSQL, ZooKeeper and Gerrit."""

import contextlib
import ctypes
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

import render_zuul_services
from bounded_file import read_bounded_bytes
from test_zookeeper_runtime_fixture import absent, command, process, show, wait
from test_zuul_config import fixture
from test_zuul_zookeeper_fixture import CANDIDATE_LOCK, checked_environment

PG_LOCK='fcb3425dc01ded8a3c9c72a3245da4104360c263a9bedf4b7320aa389c0cdb66'


def child(settings_path):
    settings=json.loads(Path(settings_path).read_bytes());parent=settings['owner'];info=show(parent['unit'])
    if info.get('InvocationID')!=parent['invocation_id'] or info.get('MainPID')!=str(parent['process']['pid']):raise ValueError('Original owner changed')
    ld_cache=Path('/etc/ld.so.cache').read_bytes()
    os.unshare(os.CLONE_NEWNS|os.CLONE_NEWUTS);command(['mount','--make-rprivate','/']);socket.sethostname(settings['identity'].encode())
    command(['mount','-t','tmpfs','-o','size=4m,mode=755','tmpfs','/etc'])
    Path('/etc/hosts').write_text(settings['hosts']);Path('/etc/nsswitch.conf').write_text('hosts: files\npasswd: files\ngroup: files\n')
    Path('/etc/ld.so.cache').write_bytes(ld_cache)
    Path('/etc/resolv.conf').write_text('nameserver 127.0.0.1\n')
    Path('/etc/localtime').symlink_to('/usr/share/zoneinfo/Etc/UTC')
    uid=settings['uid'];Path('/etc/passwd').write_text(f'zuul:x:{uid}:{uid}:Zuul:/var/lib/zuul:/bin/false\n');Path('/etc/group').write_text(f'zuul:x:{uid}:\n')
    Path('/etc/zuul').mkdir();command(['mount','--bind',settings['config'],'/etc/zuul']);command(['mount','-o','remount,bind,ro','/etc/zuul'])
    command(['mount','-t','tmpfs','-o','size=1m,mode=755','tmpfs','/var/lib']);Path('/var/lib/zuul').mkdir();command(['mount','--bind',settings['state'],'/var/lib/zuul'])
    command(['mount','-t','tmpfs','-o','size=64m,mode=1777','tmpfs','/tmp'])
    environment={**settings['environment'],'PATH':settings['sdk_bin']+':/usr/bin:/bin','HOME':'/var/lib/zuul','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','REQUESTS_CA_BUNDLE':'/etc/zuul/private/gerrit-ca.pem'}
    os.sched_setaffinity(0,set(settings['cpus']));os.setgroups([]);libc=ctypes.CDLL(None,use_errno=True)
    for capability in range(64):libc.prctl(24,capability,0,0,0)
    os.setgid(uid);os.setuid(uid)
    if libc.prctl(38,1,0,0,0):raise OSError('NoNewPrivileges failed')
    os.chdir('/var/lib/zuul')
    # Exact packaged launch guard, not an in-process imitation of the daemon.
    program=settings['sdk_bin']+'/python'
    os.execve(program,[program,'-B','/etc/zuul/start_zuul.py','--role',settings['role'],'--start','--expected-version','14.3.0'],environment)


def sql(pg,statement,database='postgres'):
    env={'PATH':'/usr/bin:/bin','LC_ALL':'C','HOME':str(pg['root']),'LD_LIBRARY_PATH':pg['libraries']}
    result=command(['/usr/bin/setpriv','--reuid=65534','--regid=65534','--clear-groups',str(pg['binaries']/'psql'),'-XAtq','--no-password','-v','ON_ERROR_STOP=1','-h',str(pg['root']/'socket-1'),'-p',str(pg['primary']['port']),'-U','postgres','-d',database],input=statement,env=env,check=False)
    if result.returncode:raise ValueError('Private application SQL fixture failed')
    return result.stdout.strip()


def web_request(address,path):
    paths={'/api/info','/api/components','/api/connections','/api/tenants','/api/tenant/platform/pipelines','/api/tenant/platform/providers','/api/tenant/platform/config-errors','/api/tenant/platform/nodes','/api/tenant/platform/nodeset-requests','/api/tenant/platform/builds'}
    if address not in {'127.0.0.101','127.0.0.102'} or path not in paths and not re.fullmatch(r'/api/tenant/platform/buildset/[0-9a-f]{32}',path):raise ValueError('Bound real web request')
    connection=http.client.HTTPConnection(address,9000,timeout=3)
    try:
        connection.request('GET',path);response=connection.getresponse();data=response.read(1024**2+1)
        if response.status!=200 or len(data)>1024**2:raise ValueError('Real web endpoint was not ready')
        return json.loads(data)
    finally:connection.close()


def pg_artifacts():
    environment=Path(os.environ['ZUUL_WEB_TEST_POSTGRES']).resolve(strict=True);path=environment/'runtime-lock.json';payload=read_bounded_bytes(path,max_bytes=2*1024**2)
    if hashlib.sha256(payload).hexdigest()!=PG_LOCK:raise ValueError('Require reviewed local PostgreSQL artifacts')
    lock=json.loads(payload);actual={str(p.relative_to(environment)) for p in environment.rglob('*') if p.is_file() and not p.is_symlink() and p!=path}
    if actual!=set(lock['files_sha256']):raise ValueError('PostgreSQL file closure changed')
    for name,digest in lock['files_sha256'].items():
        if hashlib.sha256(read_bounded_bytes(environment/name,max_bytes=64*1024**2)).hexdigest()!=digest:raise ValueError('PostgreSQL artifact changed')
    return environment


def publish_configuration(test,gerrit,key,integrated=False,workers=None,marker=None,inflight_recovery=False,post_handoff=False):
    api=gerrit['api'];user=api.bootstrap('ci-read')
    if not user.get('username'):api.call('ci-read','/a/accounts/self/username',{'username':'ci-read'},method='PUT')
    registered=api.call('ci-read','/a/accounts/self/sshkeys',key.with_suffix('.pub').read_bytes(),method='POST',content_type='text/plain');test.assertTrue(registered['valid'])
    group=api.call('fixture-admin','/a/groups/zuul-startup-readers',{},method='PUT')['id']
    api.call('fixture-admin','/a/groups/'+quote(group,safe='')+'/members/'+str(user['_account_id']),{},method='PUT')
    allow={'rules':{group:{'action':'ALLOW'}}}
    api.call('fixture-admin','/a/projects/All-Projects/access',{'add':{'GLOBAL_CAPABILITIES':{'permissions':{'streamEvents':allow}},'refs/meta/config':{'permissions':{'read':allow}}}})
    caps=api.call('ci-read','/a/accounts/self/capabilities');test.assertTrue(caps.get('streamEvents'));test.assertFalse(caps.get('administrateServer',False))
    for project in ('platform/ci-config','applications/example'):
        encoded=quote(project,safe='');api.call('fixture-admin','/a/projects/'+encoded,{'create_empty_commit':True,'branches':['main']},method='PUT')
        api.call('fixture-admin','/a/projects/'+encoded+'/access',{'add':{'refs/*':{'permissions':{'read':allow}},'refs/meta/config':{'permissions':{'read':allow}}}})
    # A real trusted config commit is consumed by real mergers/schedulers.
    # Its job is configured but no review event is sent to execute it here.
    configuration=b"""- pipeline:
    name: startup-check
    manager: independent
    trigger:
      gerrit:
        - event: patchset-created
- job:
    name: startup-probe
    parent: null
    protected: true
    final: true
    run: playbooks/startup.yaml
    nodeset:
      nodes:
        - name: worker
          label: platform-linux
- project:
    name: 127.0.0.1/applications/example
    startup-check:
      jobs:
        - startup-probe
"""
    if post_handoff:
        configuration+=b"""    handoff-post:
      jobs:
        - startup-probe
- pipeline:
    name: handoff-post
    manager: independent
    post-review: true
    trigger:
      gerrit:
        - event: ref-updated
          ref: ^(refs/heads/)?main$
"""
    if integrated:
        import render_zuul_integrated_static
        from test_nodepool_static import plans
        inventory,ci=plans();ci['zuul_version']='14.3.0'
        if workers:
            for index,worker in enumerate(workers):
                inventory['pools'][0]['nodes'][index].update(port=worker['port'],host_key=worker['host_key'],username=worker.get('username','zuul'))
        configuration += render_zuul_integrated_static.fragments(inventory,ci)['platform'].encode()
    playbook=b"""- hosts: worker
  gather_facts: false
  become: false
  tasks:
    - ansible.builtin.command:
        argv: [/usr/bin/python3, -c, 'print("startup probe")']
      changed_when: false
"""
    if gerrit.get('gitops_worker_deployment') is not None:
        # Copy only the reviewed inputs, never repository credentials or .git.
        playbook+=b"""    - ansible.builtin.file:
        path: /tmp/zuul-serving-{{ zuul.build }}/deploy/staging
        state: directory
        mode: '0700'
    - ansible.builtin.copy:
        src: '{{ zuul.executor.work_root }}/{{ zuul.project.src_dir }}/{{ item }}'
        dest: /tmp/zuul-serving-{{ zuul.build }}/{{ item }}
        mode: '0600'
      loop: [deploy/staging/web.json, deploy/staging/kustomization.yaml, test_deployment.py]
    - ansible.builtin.command:
        argv: [/usr/bin/python3, -m, unittest, -v, test_deployment.py]
        chdir: /tmp/zuul-serving-{{ zuul.build }}
      changed_when: false
"""
    if marker is not None:
        if inflight_recovery:
            code=("import time,sys; from pathlib import Path\n"
                  f"p=Path('/evidence'); v='{marker}-{{{{ zuul.change }}}}-{{{{ zuul.build }}}}'\n"
                  f"(p/'started-{{{{ zuul.build }}}}').write_text(v)\n"
                  "deadline=time.monotonic()+600\n"
                  "while not (p/'release').exists() and time.monotonic()<deadline: time.sleep(.25)\n"
                  "if not (p/'release').exists(): sys.exit(2)\n"
                  f"(p/'done-{{{{ zuul.build }}}}').write_text(v)\n")
        else:
            identity="{{ zuul.change | default(zuul.newrev) }}" if post_handoff else "{{ zuul.change }}"
            code=f"from pathlib import Path; Path('/evidence/marker').write_text('{marker}-{identity}')"
        playbook+=('    - ansible.builtin.command:\n        argv: '+json.dumps(['/usr/bin/python3','-c',code])+'\n').encode()
    change=api.call('fixture-admin','/a/changes/',{'project':'platform/ci-config','branch':'main','subject':'Private runtime startup configuration','work_in_progress':False});number=str(change['_number'])
    for name,body in (('zuul.yaml',configuration),('playbooks/startup.yaml',playbook)):
        api.call('fixture-admin','/a/changes/'+number+'/edit/'+quote(name,safe=''),body,method='PUT',content_type='text/plain')
    api.call('fixture-admin','/a/changes/'+number+'/edit:publish',{'notify':'NONE'})
    api.call('fixture-admin','/a/changes/'+number+'/revisions/current/review',{'labels':{'Code-Review':2},'notify':'NONE'})
    api.call('fixture-admin','/a/changes/'+number+'/submit',{'notify':'NONE'})
    branch=api.call('fixture-admin','/a/projects/platform%2Fci-config/branches/main')
    gerrit['zuul_startup_configuration']=configuration
    return {'trusted_branch_revision':branch['revision'],'config_sha256':hashlib.sha256(configuration).hexdigest(),'playbook_sha256':hashlib.sha256(playbook).hexdigest(),'ci_stream_events_capability_verified':True,'ci_admin_capability':False}


@contextlib.contextmanager
def web_pair(context,pg,gerrit,sdk_bin,owner,integrated=False,workers=None,executor_startup=False,real_job=False,executor_recovery=False,inflight_recovery=False,post_handoff=False,post_controllers=(),static_publication=None):
    with contextlib.ExitStack() as worker_stack:
        with web_pair_inner(context,pg,gerrit,sdk_bin,owner,integrated,workers,executor_startup,real_job,executor_recovery,inflight_recovery,worker_stack,post_handoff,post_controllers,static_publication) as probe:
            yield probe


@contextlib.contextmanager
def web_pair_inner(context,pg,gerrit,sdk_bin,owner,integrated,workers,executor_startup,real_job,executor_recovery,inflight_recovery,worker_stack,post_handoff,post_controllers,static_publication):
    test=context['test'];root=context['root'];oracle=context['oracle'];plan=fixture();plan['zuul_version']='14.3.0'
    plan['zookeeper']=[{'host':m['host'],'port':context['plan']['client_port']} for m in context['plan']['members']]
    if integrated:
        plan['tenants'][0]['allowed_labels']=['platform-linux']
        plan['integrated_launcher']={'enabled':True,'nodepool_fallback':False,'provider_projects':{'platform':'platform/ci-config'}}
    endpoint=gerrit['ssh_endpoint'];plan['gerrit']={'host':endpoint['host'],'port':endpoint['port'],'https_port':urlsplit(gerrit['origin']).port,'user':'ci-read'}
    bundle=render_zuul_services.bundle(plan);handles=[];states={};configs={};records=[]
    report={'components':records,'phases':[],'schema_initialization_verified':False,'scheduler_execution_verified':False,'database_promotion_verified':False,'physical_ha_verified':False}
    context['report']['actual_web_pair']=report
    password=secrets.token_urlsafe(24)+':%@/?';keystore=secrets.token_urlsafe(36)+'-%(LITERAL)s'
    key=root/'web-fixture-key';command(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)])
    marker=secrets.token_hex(16) if real_job else None
    if real_job:
        from test_zuul_worker_runtime_fixture import job_worker
        source=Path(os.environ['ZUUL_WORKER_PACKAGE_DIR']);installed=Path(os.environ['ZUUL_WORKER_EXTRACTED'])
        workers=[worker_stack.enter_context(job_worker(key.with_suffix('.pub'),source,installed)) for _ in range(2)]
    report['published_configuration']=publish_configuration(test,gerrit,key,integrated=integrated,workers=workers,marker=marker,inflight_recovery=inflight_recovery,post_handoff=post_handoff)
    if post_controllers:
        import test_zuul_gitops_live_fixture as gitops
        gitops.prepare(gerrit,post_controllers)
    # Deliberately exercise raw percent signs and encoded URI fields through
    # the real upstream ConfigParser, SQL driver and HTTP authentication.
    new=gerrit['users']['ci-read']
    sql(pg,"CREATE ROLE zuul_fixture LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '"+password+"';\nCREATE DATABASE zuul_fixture OWNER zuul_fixture;\n")
    for i in (1,2,3):
        data=pg['root']/('data-'+str(i));path=data/'pg_hba.conf'
        with path.open('a') as stream:stream.write('hostssl zuul_fixture zuul_fixture 127.0.0.1/32 scram-sha-256\n')
        command(['/usr/bin/setpriv','--reuid=65534','--regid=65534','--clear-groups',str(pg['binaries']/'pg_ctl'),'-D',str(data),'reload'],env={'PATH':'/usr/bin:/bin','LD_LIBRARY_PATH':pg['libraries']})
    pins=''.join('['+endpoint['host']+']:'+str(endpoint['port'])+' '+' '.join(p.read_text().split()[:2])+'\n' for p in sorted((gerrit['site']/'etc').glob('ssh_host_*_key.pub')))
    test.assertTrue(pins)
    def owned(path,content,uid):
        path.write_bytes(content);path.chmod(0o600);os.chown(path,uid,uid)
    roles={'merger':60400,'scheduler':60300,'web':60200}
    if integrated:roles['launcher']=60500
    if executor_startup:roles['executor']=60600
    for role,i in ((role,i) for role in roles for i in (1,2)):
        identity='zuul-'+role+'-'+str(i);uid=roles[role]+i;cfg=root/(identity+'-config');cfg.mkdir(mode=0o755);private=cfg/'private';private.mkdir(mode=0o700);os.chown(private,uid,uid)
        state=root/(identity+'-state');state.mkdir(mode=0o700);os.chown(state,uid,uid);configs[role,i]=cfg;states[role,i]=state
        if role=='executor':
            (state/'builds').mkdir(mode=0o700);os.chown(state/'builds',uid,uid)
        for name,content in bundle.items():
            if name.endswith('.py') or name in {'zuul.conf','tenants.yaml'}:
                (cfg/name).write_text(content);(cfg/name).chmod(0o644)
        # Native fixture co-hosts two loopback addresses at the production port;
        # only the bind address differs from the rendered deployment contract.
        config=(cfg/'zuul.conf').read_text().replace('listen_address = 0.0.0.0','listen_address = 127.0.0.10'+str(i))
        if real_job and role=='executor':
            interpreter_root=str(sdk_bin.parent.parent/'python')
            config=config.replace('manage_ansible = false\n',f'manage_ansible = false\ntrusted_ro_paths = {interpreter_root}\nuntrusted_ro_paths = {interpreter_root}\n')
        (cfg/'zuul.conf').write_text(config);(cfg/'zuul.conf').chmod(0o644)
        for name,source in (('zk-client.pem',context['cert']/'zuul.pem'),('zk-client.key',context['cert']/'zuul.key'),('zk-ca.pem',context['cert']/'client-ca.pem'),('postgres-ca.pem',pg['root']/'ca.crt'),('gerrit-ca.pem',gerrit['tls']/'ca.crt'),('gerrit-ssh.key',key)):
            owned(private/name,source.read_bytes(),uid)
        owned(private/'known_hosts',pins.encode(),uid)
        if role=='executor':owned(private/'build-ssh.key',key.read_bytes(),uid)
    host_roles=['web','scheduler','merger']+(['launcher'] if integrated else [])+(['executor'] if executor_startup else [])
    hosts='127.0.0.1 localhost\n'+''.join('127.0.0.'+str(100+10*index+i)+' zuul-'+role+'-'+str(i)+'\n' for index,role in enumerate(host_roles) for i in (1,2))+''.join(m['bind_address']+' '+m['host']+'\n' for m in context['plan']['members'])
    if workers:
        hosts+='127.0.0.1 worker-0.example.com worker-1.example.com\n'
    def start(i,role='web'):
        identity='zuul-'+role+'-'+str(i);uid=roles[role]+i;unit='platform-zuul-web-'+uuid4().hex+'.service'
        query=urlencode({'sslmode':'verify-full','sslrootcert':'/etc/zuul/private/postgres-ca.pem','application_name':identity})
        uri='postgresql://zuul_fixture:'+quote(password,safe='')+'@127.0.0.1:'+str(pg['primary']['port'])+'/zuul_fixture?'+query
        environment={'ZUUL_DATABASE_URI':uri,'ZUUL_KEYSTORE_PASSWORD':keystore,'ZUUL_GERRIT_HTTP_PASSWORD':new}
        if role=='executor':environment['ZUUL_EXECUTOR_HOSTNAME']=identity
        settings={'owner':owner,'identity':identity,'uid':uid,'hosts':hosts,'role':role,'config':str(configs[role,i]),'state':str(states[role,i]),'sdk_bin':str(sdk_bin),'cpus':sorted(os.sched_getaffinity(0))[:4],'environment':environment}
        path=root/(unit+'.json');path.write_text(json.dumps(settings));path.chmod(0o600);log=root/(unit+'.log')
        runtime_limit='720' if executor_recovery or inflight_recovery else '300'
        if os.environ.get('ZUUL_RKE2_SERVING_TEST_RUNTIME')=='1':runtime_limit='3500'
        if static_publication is not None:runtime_limit='1200'
        args=['systemd-run','--quiet','--collect','--service-type=exec','--unit='+unit,'--property=RuntimeMaxSec='+runtime_limit,'--property=TimeoutStopSec=10','--property=MemoryMax=4G','--property=MemorySwapMax=0','--property=CPUQuota=400%','--property=TasksMax=4096','--property=KillMode=control-group','--property=NoNewPrivileges=yes','--property=BindsTo='+owner['unit'],'--property=After='+owner['unit'],'--property=NetworkNamespacePath=/proc/'+str(os.getpid())+'/ns/net','--property=StandardOutput=append:'+str(log),'--property=StandardError=inherit',sys.executable,'-B',str(Path(__file__).resolve()),'child',str(path)]
        command(args);info=show(unit);pid=int(info['MainPID']);handle={'identity':identity,'role':role,'index':i,'unit':unit,'invocation_id':info['InvocationID'],'process':process(pid),'log_path':str(log),'removed':False};handles.append(handle);records.append(handle);return handle
    def live(handle):
        info=show(handle['unit']);test.assertEqual(info.get('InvocationID'),handle['invocation_id']);test.assertEqual(info.get('MainPID'),str(handle['process']['pid']));test.assertEqual(info.get('MemoryMax'),str(4*1024**3));test.assertEqual(info.get('TasksMax'),'4096');test.assertEqual(info.get('NRestarts'),'0')
        test.assertEqual(process(handle['process']['pid']),handle['process'])
        status=Path('/proc',str(handle['process']['pid']),'status').read_text();test.assertRegex(status,r'(?m)^Uid:\s+'+str(roles[handle['role']]+handle['index'])+r'\s');test.assertRegex(status,r'(?m)^CapEff:\s+0+$');test.assertRegex(status,r'(?m)^NoNewPrivs:\s+1$')
        handle['runtime_status']=status
    def ready(handle):
        if absent(handle['process']):raise ValueError('Original guarded web daemon exited')
        try:return web_request('127.0.0.10'+str(handle['index']),'/api/info')
        except (OSError,ValueError,http.client.HTTPException):return False
    def components(role='web'):
        result={}
        path='/zuul/components/'+role
        if not oracle.exists(path):return result
        for name in oracle.get_children(path):
            data,stat=oracle.get(path+'/'+name);row=json.loads(data);result[row['hostname']]={'path':path+'/'+name,'session_id':stat.ephemeralOwner,'state':row['state'],'version':row['version']}
        return result
    def stop(handle,kill=False):
        if handle['removed']:return
        info=show(handle['unit'])
        if info.get('LoadState')!='not-found':
            test.assertEqual(info.get('InvocationID'),handle['invocation_id'])
            command(['systemctl','kill','--kill-whom=all','--signal=KILL',handle['unit']] if kill else ['systemctl','stop',handle['unit']],check=False)
        wait(lambda:absent(handle['process']),20);handle['removed']=True
    try:
        print(json.dumps({'zuul_web_stage':'guarded-pair-start'}),flush=True)
        dependencies=[start(i,role) for role in (('merger','scheduler','launcher') if integrated else ('merger','scheduler')) for i in (1,2)]
        if executor_startup:dependencies.append(start(1,'executor'))
        first,second=start(1),start(2)
        for handle in (first,second):wait(lambda handle=handle:ready(handle),180);live(handle)
        registered=wait(lambda:components() if len(components())==2 and all(v['state']=='running' for v in components().values()) else False,30)
        test.assertEqual(set(registered),{'zuul-web-1','zuul-web-2'});test.assertEqual(len({r['session_id'] for r in registered.values()}),2);test.assertTrue(all(row['version']=='14.3.0' for row in registered.values()));report['initial_components']=registered
        for role in (('merger','scheduler','launcher') if integrated else ('merger','scheduler')):
            def role_ready():
                values=components(role)
                return values if set(values)=={'zuul-'+role+'-1','zuul-'+role+'-2'} and all(c['state']=='running' for c in values.values()) else False
            observed=wait(role_ready,90)
            report['initial_'+role+'_components']=observed
        for handle in dependencies:live(handle)
        if executor_startup:
            def executor_ready():
                values=components('executor')
                return values if set(values)=={'zuul-executor-1'} and values['zuul-executor-1']['state']=='running' else False
            observed=wait(executor_ready,90)
            report['actual_guarded_executor_startup_verified']=True
            if static_publication is not None:
                # Keep this fixture's job logs until its owning temporary scope
                # is removed. Only scrubbed logs may leave that private scope.
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as control:
                    control.settimeout(3);control.connect(str(states['executor',1]/'executor.socket'));control.sendall(b'keep\n')
        tenants=web_request('127.0.0.101','/api/tenants');test.assertEqual([t['name'] for t in tenants],['platform'])
        pipelines=web_request('127.0.0.101','/api/tenant/platform/pipelines');test.assertTrue(any(p['name']=='startup-check' for p in pipelines))
        if integrated:
            errors=web_request('127.0.0.101','/api/tenant/platform/config-errors');test.assertFalse(errors)
            providers=web_request('127.0.0.101','/api/tenant/platform/providers')
            test.assertTrue(any(p.get('name')=='platform-linux' for p in providers))
            report['actual_integrated_provider_and_launcher_pair_verified']=True
            def ready_nodes():
                nodes=web_request('127.0.0.101','/api/tenant/platform/nodes')
                matches=[n for n in nodes if n.get('label')=='platform-linux' and n.get('state')=='ready']
                return matches if len(matches)>=2 else False
            ready_workers=wait(ready_nodes,90)
            test.assertEqual(len({n['uuid'] for n in ready_workers}),2)
            report['actual_static_nodes_ready_verified']=True
            api=gerrit['api']
            change=api.call('fixture-admin','/a/changes/',{'project':'applications/example','branch':'main','subject':'Disposable Zuul allocation test','work_in_progress':False})
            number=str(change['_number'])
            api.call('fixture-admin','/a/changes/'+number+'/edit/allocation-test.txt',b'Private test fixture\n',method='PUT',content_type='text/plain')
            if post_controllers:
                gitops.add_manifest(gerrit,number)
            api.call('fixture-admin','/a/changes/'+number+'/edit:publish',{'notify':'NONE'})
            serving_patchset=None
            if gerrit.get('gitops_worker_deployment') is not None:
                detail=api.call('ci-read','/a/changes/'+number+'?o=CURRENT_REVISION')
                serving_patchset=str(detail['revisions'][detail['current_revision']]['_number'])
            def allocated():
                requests=web_request('127.0.0.101','/api/tenant/platform/nodeset-requests')
                nodes=web_request('127.0.0.101','/api/tenant/platform/nodes')
                relevant=[r for r in requests if r.get('job_name')=='startup-probe' and r.get('pipeline_name')=='startup-check']
                request_ids={r['uuid'] for r in relevant}
                assigned=[n for n in nodes if n.get('label')=='platform-linux' and n.get('request_id') in request_ids]
                return (relevant,assigned) if relevant and assigned else False
            if real_job:
                def finished():
                    builds=web_request('127.0.0.101','/api/tenant/platform/builds')
                    matches=[b for b in builds if b.get('job_name')=='startup-probe' and str((b.get('ref') or {}).get('change'))==number and b.get('result')]
                    if serving_patchset is not None:
                        matches=[b for b in matches if str((b.get('ref') or {}).get('patchset'))==serving_patchset]
                    if inflight_recovery:matches=[b for b in matches if b.get('result')=='SUCCESS' and b.get('uuid')!=first_build_id]
                    return matches[0] if matches else False
                if inflight_recovery:
                    def first_started():
                        paths=[p for w in workers for p in w['evidence'].glob('started-*') if p.is_file()]
                        return paths[0] if len(paths)==1 else False
                    started=wait(first_started,180)
                    first_build_id=started.name.removeprefix('started-')
                    test.assertRegex(first_build_id,r'^[a-f0-9]{32}$')
                    test.assertEqual(read_bounded_bytes(started,max_bytes=128).decode(),marker+'-'+number+'-'+first_build_id)
                    test.assertEqual(started.stat().st_uid,60601)
                    test.assertFalse(any(list(w['evidence'].glob('done-*')) for w in workers))
                    original_executor=next(h for h in dependencies if h['role']=='executor')
                    old_session=components('executor')['zuul-executor-1']['session_id']
                    stop(original_executor,kill=True)
                    wait(lambda:'zuul-executor-1' not in components('executor'),160)
                    replacement=start(1,'executor')
                    dependencies[dependencies.index(original_executor)]=replacement
                    def running_executor():
                        value=components('executor').get('zuul-executor-1')
                        return value if value and value['state']=='running' else False
                    recovered=wait(running_executor,90)
                    test.assertNotEqual(recovered['session_id'],old_session)
                    live(replacement)
                    for worker in workers:(worker['evidence']/'release').write_text('release\n')
                build=wait(finished,300 if inflight_recovery else 180)
                report['actual_job_result']={'job_name':build.get('job_name'),'result':build.get('result'),'change':number}
                test.assertEqual(build.get('result'),'SUCCESS')
                marker_name='done-'+build['uuid'] if inflight_recovery else 'marker'
                markers=[w['evidence']/marker_name for w in workers if (w['evidence']/marker_name).is_file()]
                test.assertEqual(len(markers),1)
                expected_marker=marker+'-'+number+('-'+build['uuid'] if inflight_recovery else '')
                test.assertEqual(read_bounded_bytes(markers[0],max_bytes=128).decode(),expected_marker)
                test.assertEqual(markers[0].stat().st_uid,60601)
                report['actual_worker_marker_and_nonroot_identity_verified']=True
                report['actual_gerrit_job_executed_verified']=True
                if post_handoff:
                    from zuul_gitops_handoff import verify_buildset
                    markers[0].unlink()
                    api.call('fixture-admin','/a/changes/'+number+'/revisions/current/review',{'labels':{'Code-Review':2},'notify':'NONE'})
                    api.call('fixture-admin','/a/changes/'+number+'/submit',{'wait_for_merge':True,'notify':'NONE'})
                    revision=api.call('ci-read','/a/projects/applications%2Fexample/branches/main')['revision']
                    def finished_post():
                        for job in web_request('127.0.0.101','/api/tenant/platform/builds'):
                            if (job.get('job_name')=='startup-probe' and job.get('pipeline')=='handoff-post'
                                    and job.get('ref',{}).get('newrev')==revision and job.get('final') is True):
                                buildset=job['buildset']['uuid']
                                row=web_request('127.0.0.101','/api/tenant/platform/buildset/'+buildset)
                                if row.get('result'):
                                    return buildset
                        return False
                    buildset=wait(finished_post,180)
                    policy={'tenant':'platform','pipeline':'handoff-post','project':'applications/example',
                            'branch':'main','revision':revision,'uuid':buildset,'required_jobs':['startup-probe']}
                    proof=verify_buildset(policy,lambda path:web_request('127.0.0.101',path))
                    post_markers=[worker['evidence']/'marker' for worker in workers if (worker['evidence']/'marker').is_file()]
                    test.assertEqual(len(post_markers),1)
                    test.assertEqual(read_bounded_bytes(post_markers[0],max_bytes=128).decode(),marker+'-'+revision)
                    test.assertEqual(post_markers[0].stat().st_uid,60601)
                    report['actual_post_buildset_handoff_evidence_verified']=True
                    report['post_buildset_evidence_sha256']=hashlib.sha256(json.dumps(proof,sort_keys=True).encode()).hexdigest()
                    for controller in post_controllers:
                        deployment=gerrit.get('gitops_worker_deployment')
                        report.update((deployment or gitops.exercise)(test,gerrit,policy,lambda path:web_request('127.0.0.101',path),controller))
                    if gerrit.get('gitops_worker_deployment') is not None:
                        report['actual_reviewed_manifest_test_on_zuul_worker_verified']=True
                if inflight_recovery:
                    test.assertNotEqual(build['uuid'],first_build_id)
                    report['actual_inflight_executor_loss_retry_verified']=True
                    report['inflight_retry']={'change':number,'initial_build':first_build_id,'successful_build':build['uuid']}
                if executor_recovery:
                    markers[0].unlink()
                    original_executor=next(h for h in dependencies if h['role']=='executor')
                    old_session=components('executor')['zuul-executor-1']['session_id']
                    stop(original_executor,kill=True)
                    wait(lambda:'zuul-executor-1' not in components('executor'),160)
                    replacement=start(1,'executor')
                    dependencies[dependencies.index(original_executor)]=replacement
                    def running_executor():
                        value=components('executor').get('zuul-executor-1')
                        return value if value and value['state']=='running' else False
                    recovered=wait(running_executor,90)
                    test.assertEqual(recovered['state'],'running')
                    test.assertNotEqual(recovered['session_id'],old_session)
                    live(replacement)
                    retry=api.call('fixture-admin','/a/changes/',{'project':'applications/example','branch':'main','subject':'Disposable executor recovery test','work_in_progress':False})
                    number=str(retry['_number'])
                    api.call('fixture-admin','/a/changes/'+number+'/edit/recovery-test.txt',b'Private recovery fixture\n',method='PUT',content_type='text/plain')
                    api.call('fixture-admin','/a/changes/'+number+'/edit:publish',{'notify':'NONE'})
                    second_build=wait(finished,180)
                    test.assertEqual(second_build.get('result'),'SUCCESS')
                    test.assertNotEqual(second_build.get('uuid'),build.get('uuid'))
                    recovered_markers=[w['evidence']/'marker' for w in workers if (w['evidence']/'marker').is_file()]
                    test.assertEqual(len(recovered_markers),1)
                    test.assertEqual(read_bounded_bytes(recovered_markers[0],max_bytes=128).decode(),marker+'-'+number)
                    test.assertEqual(recovered_markers[0].stat().st_uid,60601)
                    report['actual_executor_crash_restart_and_second_job_verified']=True
                    report['actual_recovery_job_result']={'job_name':second_build.get('job_name'),'result':second_build.get('result'),'change':number}
            else:
                requests,assigned=wait(allocated,120)
                report['actual_gerrit_change_node_request_and_assignment_verified']=bool(requests and assigned)
        if static_publication is not None:
            report['static_publication']=static_publication(test,context,gerrit,plan)
        report['actual_scheduler_priming_and_merger_config_fetch_verified']=True
        tables=json.loads(sql(pg,"SELECT json_agg(tablename ORDER BY tablename) FROM pg_tables WHERE schemaname='public';",'zuul_fixture'));test.assertTrue({'zuul_build','zuul_buildset','zuul_ref','alembic_version'}<=set(tables));test.assertEqual(sql(pg,'SELECT count(*) FROM alembic_version;','zuul_fixture'),'1');report['schema_tables']=tables;report['schema_initialization_verified']=True
        sessions=json.loads(sql(pg,"SELECT json_agg(json_build_object('pid',a.pid,'application_name',a.application_name,'tls',s.ssl,'role',a.usename)) FROM pg_stat_activity a JOIN pg_stat_ssl s USING(pid) WHERE a.usename='zuul_fixture';"));test.assertTrue({'zuul-web-1','zuul-web-2'}<={r['application_name'] for r in sessions});test.assertTrue(all(r['tls'] for r in sessions));report['actual_application_database_sessions']=sessions
        observations=gerrit['config_request_observations'];test.assertGreaterEqual(sum(r['resource']=='version' and r['user']=='ci-read' and r['status']==200 for r in observations),2);test.assertGreaterEqual(sum(r['resource']=='info' and r['user']=='ci-read' and r['status']==200 for r in observations),2);report['actual_successful_gerrit_config_requests']=list(observations)
        for handle in (first,second):
            parsed=web_request('127.0.0.10'+str(handle['index']),'/api/components');test.assertEqual(len(parsed['web']),2)
            connections=web_request('127.0.0.10'+str(handle['index']),'/api/connections');test.assertIn('gerrit',[r['name'] for r in connections])
        lost_at=time.monotonic();stop(first,kill=True);live(second);web_request('127.0.0.102','/api/info')
        # Keep the real upstream 120-second session request unchanged. A killed
        # peer's ephemeral registration remains until its negotiated lease ends.
        wait(lambda:'zuul-web-1' not in components(),160)
        live(second);test.assertEqual(len(web_request('127.0.0.102','/api/components')['web']),1)
        report['peer_loss']={'killed_monotonic':lost_at,'lease_removed_monotonic':time.monotonic(),'survivor_http_available_before_lease_expiry':True,'upstream_session_request_seconds':120}
        first=start(1);wait(lambda:ready(first),60);wait(lambda:len(components())==2,30);live(first);live(second);report['peer_loss_survivor_and_original_state_restart_verified']=True
        class Probe:
            def check(self,phase):
                for handle in (*dependencies,first,second):
                    live(handle)
                for handle in (first,second):test.assertEqual(len(web_request('127.0.0.10'+str(handle['index']),'/api/components')['web']),2)
                test.assertEqual(len(components()),2);report['phases'].append({'phase':phase,'original_running_pair_retained':True,'actual_http_components_success':True})
        probe=Probe();probe.check('initial');yield probe
        report['actual_guarded_web_pair_verified']=len(report['phases'])==3
    finally:
        for handle in reversed(handles):
            stop(handle);log=Path(handle['log_path']);handle['private_log']=read_bounded_bytes(log,max_bytes=4*1024**2).decode(errors='replace') if log.exists() else ''
            test.assertFalse(any(value in handle['private_log'] for value in (password,keystore,new)))
        report['original_web_processes_removed']=all(absent(h['process']) for h in handles)


def exercise(test,integrated=False,executor_startup=False,real_job=False,executor_recovery=False,inflight_recovery=False,post_handoff=False,post_controllers=(),post_deployment=None,static_publication=None):
    if static_publication is not None and (not callable(static_publication) or post_controllers or post_deployment is not None
            or not (integrated and executor_startup and real_job) or os.environ.get('ZUUL_STATIC_JOB_TEST_RUNTIME')!='1'):
        raise ValueError("Select the separate explicit real trusted publisher job fixture")
    if post_deployment is not None and (not callable(post_deployment) or post_controllers not in (('argocd',), ('flux',))
                                       or os.environ.get('ZUUL_RKE2_SERVING_TEST_RUNTIME')!='1'):
        raise ValueError("Select the explicit isolated RKE2 serving callback")
    if post_controllers and not (post_handoff and real_job and integrated and executor_startup):
        raise ValueError("Run native GitOps only with the integrated post-review worker fixture")
    import test_postgres_runtime_fixture as postgres
    import test_zookeeper_runtime_fixture as zookeeper
    from test_gerrit_review_runtime_fixture import server
    owner=zookeeper.owner();pgroot=pg_artifacts()
    if executor_startup:
        from zuul_fixture_integrity import fingerprint
        candidate=Path(os.environ['ZUUL_EXECUTOR_TEST_ENVIRONMENT']).resolve(strict=True)
        expected='bfdd1828a69cf41e104a65e7162cc9ba376e764803cfdb39b4bb88dce891f3d3'
        if fingerprint(candidate)['digest']!=expected:raise ValueError('Executor test runtime fingerprint changed')
        sdk_bin=candidate/'sdk/bin'
    else:
        sdk=checked_environment(Path(os.environ['ZUUL_WEB_TEST_ENVIRONMENT']),CANDIDATE_LOCK)
        sdk_bin=sdk/'venv/bin'
    project=Path(__file__).resolve().parents[1];out=Path(os.environ['ZUUL_WEB_TEST_REPORT']).resolve()
    if not out.is_relative_to(project/'private') or out.exists():raise ValueError('Require a fresh private web report')
    sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (project/'scripts').glob('*.py')};result={'source_sha256':sources,'owner':owner,'postgres_artifact_lock_sha256':PG_LOCK,'sdk_lock_sha256':expected if executor_startup else CANDIDATE_LOCK,'actual_guarded_web_pair_verified':False,'full_stack_verified':False};failure=None
    command(['ip','link','set','lo','up']);os.environ['POSTGRES_RUNTIME_TEST_BIN']=str(pgroot/'usr/lib/postgresql/18/bin');os.environ['POSTGRES_RUNTIME_TEST_LIBS']=str(pgroot/'usr/lib/x86_64-linux-gnu')
    def connected(pg):
        result['database_version']=pg['version'];result['database_system_identifier']=pg['system_identifier'];result['database_processes']=[process(int((pg['root']/('data-'+str(i))/'postmaster.pid').read_text().splitlines()[0])) for i in (1,2,3)]
        with contextlib.ExitStack() as stack:
            artifact_rollout=static_publication is not None and os.environ.get('ZUUL_ARTIFACT_ROLLOUT_TEST_RUNTIME')=='1'
            handoff=bool(post_controllers) or artifact_rollout
            gerrit=stack.enter_context(server(ssh=True,observe_config_requests=True,handoff=handoff,disk_workspace=handoff))
            if post_deployment is not None:
                gerrit['gitops_worker_deployment']=post_deployment
            workers=None
            if integrated and not real_job:
                from test_zuul_static_ssh_fixture import host_key_endpoint
                workers=[stack.enter_context(host_key_endpoint()) for _ in range(2)]
            result['gerrit_process']=process(gerrit['process'].pid)
            def coordinate(context):return web_pair(context,pg,gerrit,sdk_bin,owner,integrated=integrated,workers=workers,executor_startup=executor_startup,real_job=real_job,executor_recovery=executor_recovery,inflight_recovery=inflight_recovery,post_handoff=post_handoff,post_controllers=post_controllers,static_publication=static_publication)
            report=zookeeper.exercise(test,coordination=coordinate);result['coordination']=report;result['actual_guarded_web_pair_verified']=report['actual_web_pair']['actual_guarded_web_pair_verified']
    try:postgres.exercise(test,hook=connected)
    except Exception as error:failure=error;result['failure_type']=type(error).__name__;raise
    finally:
        if 'gerrit_process' in result:result['original_gerrit_process_removed']=absent(result['gerrit_process'])
        result['original_database_processes_removed']=all(absent(p) for p in result.get('database_processes',[]));result['sources_unchanged']=sources=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (project/'scripts').glob('*.py')};result['failed']=failure is not None
        if executor_startup:
            result['executor_sdk_unchanged']=fingerprint(candidate)['digest']==expected
        if 'coordination' not in result and Path(os.environ['ZOOKEEPER_TEST_REPORT']).exists():result['coordination']=json.loads(Path(os.environ['ZOOKEEPER_TEST_REPORT']).read_bytes())
        descriptor=os.open(out,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w') as stream:json.dump(result,stream,sort_keys=True);stream.write('\n')
    test.assertTrue(result['sources_unchanged']);test.assertTrue(result['original_database_processes_removed']);test.assertTrue(result['original_gerrit_process_removed'])
    if executor_startup:test.assertTrue(result['executor_sdk_unchanged'])
    return result


if __name__=='__main__' and len(sys.argv)==3 and sys.argv[1]=='child':child(sys.argv[2])
