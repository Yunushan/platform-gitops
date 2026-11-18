#!/usr/bin/env python3
"""Pinned upstream Zuul SSH transport against real disposable Gerrit; opt-in."""

import hashlib
import importlib.metadata
import inspect
import json
import logging
import os
import sys
from pathlib import Path

import start_zuul as launcher
from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from test_gerrit_review_runtime_fixture import server

ZUUL_VERSION="14.3.0"
ENVIRONMENT_LOCK_SHA="b637f3963c8091875d46bff828ed4be43f40886163207f91e0e003337f4b8d37"


def child(settings_path,report_path):
    import paramiko
    from zuul.driver.gerrit.gerritconnection import GerritConnection
    settings=json.loads(Path(settings_path).read_bytes());root=Path(settings['directory']);source=root/'known_hosts';config=root/'zuul.conf';state=root/'state'
    if os.geteuid()==0 or importlib.metadata.version('zuul')!=ZUUL_VERSION:raise ValueError('Require the pinned non-root Zuul runtime')
    report={'case':settings['case'],'euid':os.geteuid(),'zuul_version':importlib.metadata.version('zuul'),'paramiko_version':paramiko.__version__,'upstream_open_called':False,'source_known_hosts_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'upstream_open_source_sha256':hashlib.sha256(inspect.getsource(GerritConnection._open).encode()).hexdigest(),'upstream_ssh_source_sha256':hashlib.sha256(inspect.getsource(GerritConnection._ssh).encode()).hexdigest()}
    status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    report.update(no_new_privileges=status['NoNewPrivs'].strip(),effective_capabilities=status['CapEff'].strip(),pid=os.getpid(),start_ticks=int(Path('/proc/self/stat').read_text().rsplit(') ',1)[1].split()[19]),boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),cgroup=Path('/proc/self/cgroup').read_text().strip(),network_inode=Path('/proc/self/ns/net').stat().st_ino)
    try:launcher.verify_runtime_gerrit_host_pins(settings['scheduler_binary'],config,source)
    except launcher.ZuulLaunchError:
        if settings['expected']!='missing-pin':raise
        if (state/'.ssh').exists():raise AssertionError('Missing pin staged trust')
        report['missing_pin_refused_before_ssh']=True
    else:
        if settings['expected']=='missing-pin':raise AssertionError('Unknown endpoint pin was accepted')
        launcher.check_version(settings['scheduler_binary'],ZUUL_VERSION)
        launcher.stage_known_hosts(source,state);launcher.verify_runtime_gerrit_host_pins(settings['scheduler_binary'],config,state/'.ssh/known_hosts');os.environ['HOME']=str(state)
        connection=GerritConnection.__new__(GerritConnection)
        for name,value in {'client':None,'ssh_server':settings['host'],'port':settings['port'],'user':settings['username'],'keyfile':settings['keyfile'],'ssh_timeout':5,'keepalive':5,'log':logging.getLogger('fixture.gerrit'),'iolog':logging.getLogger('fixture.gerrit.io')}.items():setattr(connection,name,value)
        try:
            report['upstream_open_called']=True
            try:connection._open()
            except paramiko.BadHostKeyException as error:
                if settings['expected']!='wrong-pin':raise
                if connection.client is not None:raise AssertionError('Bad-key transport retained')
                report.update(changed_host_key_refused=True,presented_key_sha256=hashlib.sha256(error.key.asbytes()).hexdigest())
            else:
                if settings['expected']!='valid-pin':raise AssertionError('Changed host key connected')
                key=connection.client.get_transport().get_remote_server_key();out,err=connection._ssh('gerrit version')
                if out.strip()!='gerrit version 3.13.10' or err.strip():raise AssertionError('Actual Gerrit SSH version differed')
                report.update(authenticated_actual_gerrit_version_verified=True,presented_key_sha256=hashlib.sha256(key.asbytes()).hexdigest(),host_key_type=key.get_name())
        finally:
            if connection.client is not None:connection.client.close()
        report['staged_known_hosts_sha256']=hashlib.sha256((state/'.ssh/known_hosts').read_bytes()).hexdigest()
        if report['staged_known_hosts_sha256']!=report['source_known_hosts_sha256']:raise AssertionError('Trust bytes changed')
    if hashlib.sha256(source.read_bytes()).hexdigest()!=report['source_known_hosts_sha256']:raise AssertionError('Source trust adopted a remote key')
    Path(report_path).write_text(json.dumps(report,sort_keys=True)+'\n')


def exercise(test):
    root_project=Path(__file__).resolve().parents[1];environment=Path(os.environ['ZUUL_GERRIT_SSH_TEST_ENVIRONMENT']).resolve();report_path=Path(os.environ['ZUUL_GERRIT_SSH_TEST_REPORT']).resolve()
    if not report_path.is_relative_to(root_project/'private') or report_path.exists():raise ValueError('Require fresh private SSH proof')
    lock_bytes=read_bounded_bytes(environment/'runtime-lock.json',max_bytes=8*1024**2)
    if hashlib.sha256(lock_bytes).hexdigest()!=ENVIRONMENT_LOCK_SHA:raise ValueError('Require the reviewed pinned Zuul runtime lock')
    lock=json.loads(lock_bytes)
    if hashlib.sha256(Path(lock['python_executable']).read_bytes()).hexdigest()!=lock['python_sha256']:raise ValueError('Reviewed Python interpreter changed')
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (root_project/'scripts').glob('*.py')}
    for name,digest in lock['files_sha256'].items():
        path=environment/name
        if not path.resolve(strict=True).is_relative_to(environment) or path.is_symlink() or hashlib.sha256(read_bounded_bytes(path,max_bytes=128*1024**2)).hexdigest()!=digest:raise ValueError('Pinned Zuul runtime environment changed')
    records=[];report={'source_sha256':before,'runtime_lock_sha256':hashlib.sha256((environment/'runtime-lock.json').read_bytes()).hexdigest(),'cases':records,'zuul_scheduler_execution_verified':False,'physical_ha_verified':False,'full_stack_verified':False}
    with server(active_processors=2,ssh=True) as gerrit:
        root=gerrit['root'];api=gerrit['api'];user=api.bootstrap('ci-review');endpoint=gerrit['ssh_endpoint'];directory=root/'zuul-ssh';directory.mkdir(mode=0o700);os.chown(directory,65534,65534)
        key=directory/'client-key';run_bounded(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)],timeout=20,output_max_bytes=65536,check=True);os.chown(key,65534,65534);key.chmod(0o600)
        username=user.get('username')
        if not username:
            username='ci-review';api.call('ci-review','/a/accounts/self/username',{'username':username},method='PUT')
        registered=api.call('ci-review','/a/accounts/self/sshkeys',key.with_suffix('.pub').read_bytes(),method='POST',content_type='text/plain');test.assertTrue(registered['valid'])
        public=[]
        for path in sorted((gerrit['site']/'etc').glob('ssh_host_*_key.pub')):
            fields=path.read_text().split()
            if len(fields)>=2 and fields[0] in {'ssh-rsa','ssh-ed25519','ecdsa-sha2-nistp256','ecdsa-sha2-nistp384','ecdsa-sha2-nistp521'}:public.append(' '.join(fields[:2]))
        test.assertTrue(public);token='['+endpoint['host']+']:'+str(endpoint['port'])
        # Derive pins from this fixture's provisioned public host keys, never a network scan.
        import base64
        report['trusted_host_key_sha256']=[hashlib.sha256(base64.b64decode(line.split()[1],validate=True)).hexdigest() for line in public]
        report['gerrit_process']={'pid':gerrit['process'].pid,'start_ticks':int(Path('/proc',str(gerrit['process'].pid),'stat').read_text().rsplit(') ',1)[1].split()[19]),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'cgroup':Path('/proc',str(gerrit['process'].pid),'cgroup').read_text().strip()}
        wrong=directory/'wrong-host-key';run_bounded(['ssh-keygen','-q','-t','rsa','-b','2048','-N','','-f',str(wrong)],timeout=20,output_max_bytes=65536,check=True);wrong_pub=' '.join(wrong.with_suffix('.pub').read_text().split()[:2])
        cases=[('matching-pin','valid-pin',''.join(token+' '+line+'\n' for line in public)),('changed-pin','wrong-pin',token+' '+wrong_pub+'\n'),('missing-pin','missing-pin','[other.example.com]:'+str(endpoint['port'])+' '+public[0]+'\n'),('wrong-port','missing-pin','['+endpoint['host']+']:'+str(endpoint['port']+1 if endpoint['port']<65535 else endpoint['port']-1)+' '+public[0]+'\n')]
        for label,expected,pins in cases:
            folder=directory/label;folder.mkdir(mode=0o700);os.chown(folder,65534,65534);(folder/'state').mkdir(mode=0o700);os.chown(folder/'state',65534,65534)
            config='[connection "gerrit"]\ndriver = gerrit\nserver = '+endpoint['host']+'\nport = '+str(endpoint['port'])+'\n'
            for name,value in (('zuul.conf',config),('known_hosts',pins)):
                path=folder/name;path.write_text(value);path.chmod(0o600);os.chown(path,65534,65534)
            settings={'directory':str(folder),'case':label,'expected':expected,'host':endpoint['host'],'port':endpoint['port'],'username':username,'keyfile':str(key),'scheduler_binary':str(environment/'venv/bin/zuul-scheduler')};input_path=folder/'input.json';input_path.write_text(json.dumps(settings));os.chown(input_path,65534,65534);input_path.chmod(0o600);output_path=folder/'report.json'
            print(json.dumps({'zuul_ssh_stage':label}),flush=True)
            result=run_bounded([sys.executable,'-B',str(Path(__file__).with_name('test_rook_api_fixture.py')),'--child',str(os.getpid()),str(environment/'venv/bin/python'),'-B',str(Path(__file__).resolve()),'child',str(input_path),str(output_path)],text=True,timeout=60,output_max_bytes=1024*1024,check=False)
            if result.returncode:
                report['failure']={'case':label,'returncode':result.returncode,'private_stderr':result.stderr};break
            record=json.loads(output_path.read_bytes());test.assertEqual(record['euid'],65534);test.assertEqual(record['no_new_privileges'],'1');test.assertEqual(int(record['effective_capabilities'],16),0)
            if expected=='valid-pin':test.assertTrue(record['authenticated_actual_gerrit_version_verified']);test.assertIn(record['presented_key_sha256'],report['trusted_host_key_sha256'])
            elif expected=='wrong-pin':test.assertTrue(record['changed_host_key_refused']);test.assertIn(record['presented_key_sha256'],report['trusted_host_key_sha256'])
            else:test.assertTrue(record['missing_pin_refused_before_ssh']);test.assertFalse(record['upstream_open_called'])
            records.append(record)
    report['original_gerrit_process_and_scope_removed']=gerrit['process'].poll() is not None and not root.exists();report['sources_unchanged']=before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (root_project/'scripts').glob('*.py')};report['actual_pinned_zuul_gerrit_ssh_transport_verified']=len(records)==4 and 'failure' not in report
    descriptor=os.open(report_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as output:json.dump(report,output,sort_keys=True);output.write('\n')
    test.assertNotIn('failure',report);test.assertTrue(report['original_gerrit_process_and_scope_removed']);test.assertTrue(report['sources_unchanged']);return report


if __name__=='__main__' and len(sys.argv)==4 and sys.argv[1]=='child':child(sys.argv[2],sys.argv[3])
