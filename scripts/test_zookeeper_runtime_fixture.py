#!/usr/bin/env python3
"""Opt-in real TLS ZooKeeper quorum; isolated processes do not prove host HA."""

import contextlib
import ctypes
import hashlib
import importlib
import json
import logging
import os
import re
import socket
import ssl
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from uuid import uuid4

import render_zookeeper as renderer
import start_zookeeper as starter
from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from test_zookeeper import plans

VERSION="3.9.7"
PINS={"apache-zookeeper-3.9.7-bin.tar.gz":"d87db41fc53cf06966497489be6ee604b9bbd5f53871183a252371db75f7617c",
      "kazoo-2.11.0-py2.py3-none-any.whl":"aa6be17159540dae4087661b8d4e24c7b58d4dc26170a2d5b8468b06712d84a9"}
JAVA_SHA="a535a1df9c6284d9618872984e026ae0352d1f7e9e0605425c0e6114f47351dd"


def command(args,**kwargs):
    return run_bounded(args,text=True,timeout=30,output_max_bytes=1024*1024,**kwargs)


def pinned(path,digest,limit):
    if path.is_symlink() or hashlib.sha256(read_bounded_bytes(path,max_bytes=limit)).hexdigest()!=digest:
        raise ValueError("Require the pinned original runtime artifact")


def process(pid):
    root=Path('/proc')/str(pid)
    return {'pid':pid,'start_ticks':int((root/'stat').read_text().rsplit(') ',1)[1].split()[19]),
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'cgroup':(root/'cgroup').read_text().strip(),
            'network_inode':(root/'ns/net').stat().st_ino}


def absent(original):
    try:return process(original['pid'])['start_ticks']!=original['start_ticks'] or Path('/proc/sys/kernel/random/boot_id').read_text().strip()!=original['boot_id']
    except (FileNotFoundError,ProcessLookupError):return True


def wait(predicate,seconds=60):
    deadline=time.monotonic()+seconds
    while True:
        value=predicate()
        if value:
            if time.monotonic()>deadline:raise TimeoutError('Late quorum observation')
            return value
        if time.monotonic()>=deadline:raise TimeoutError('Original quorum observation deadline')
        time.sleep(.25)


def owner():
    group=Path('/proc/self/cgroup').read_text().strip()
    match=re.fullmatch(r'0::/system.slice/(platform-zookeeper-runtime-[a-z0-9-]+[.]service)',group)
    if match is None and os.environ.get('ZUUL_RKE2_SERVING_TEST_RUNTIME')=='1':
        match=re.fullmatch(r'0::/system.slice/(platform-k3s-vm-zuul-rke2-[a-z0-9-]+[.]service)',group)
    if match is None and os.environ.get('ZUUL_ARTIFACT_ROLLOUT_TEST_RUNTIME')=='1':
        match=re.fullmatch(r'0::/system.slice/(platform-k3s-vm-zuul-artifact-[a-z0-9-]+[.]service)',group)
    if not match or os.geteuid()!=0 or Path('/proc/self/ns/net').stat().st_ino==Path('/proc/1/ns/net').stat().st_ino:
        raise ValueError('Require a bounded original isolated ZooKeeper runtime unit')
    unit=match.group(1);info=show(unit)
    if info.get('ActiveState')!='active' or info.get('MainPID')!=str(os.getpid()):raise ValueError('Original runtime owner changed')
    if unit.startswith('platform-k3s-vm-zuul-'):
        scope=Path('/sys/fs/cgroup/system.slice')/unit
        for name,limit in (('memory.max',12*1024**3),('pids.max',4096)):
            value=(scope/name).read_text().strip()
            if not value.isdigit() or not 0<int(value)<=limit:raise ValueError('Require bounded combined Zuul/RKE2 resources')
    return {'unit':unit,'invocation_id':info['InvocationID'],'process':process(os.getpid())}


def show(unit):
    raw=command(['systemctl','show','--all',unit],check=False).stdout
    return dict(line.split('=',1) for line in raw.splitlines() if '=' in line)


def child(path):
    settings=json.loads(Path(path).read_bytes());scope=Path(settings['root']);pid=os.getpid()
    if scope.parent!=Path('/var/tmp') or not re.fullmatch('platform-zookeeper-runtime-[a-z0-9_]+',scope.name) or scope.is_symlink():raise ValueError('Wrong private member scope')
    original=settings['owner'];info=show(original['unit'])
    if info.get('InvocationID')!=original['invocation_id'] or info.get('MainPID')!=str(original['process']['pid']):raise ValueError('Original parent changed')
    os.unshare(os.CLONE_NEWNS)
    if settings.get('network_path'):
        descriptor=os.open(settings['network_path'],os.O_RDONLY)
        try:os.setns(descriptor,os.CLONE_NEWNET)
        finally:os.close(descriptor)
    else:os.unshare(os.CLONE_NEWNET)
    command(['mount','--make-rprivate','/'])
    command(['mount','-t','tmpfs','-o','size=4m,mode=755','tmpfs','/etc'])
    Path('/etc/hosts').write_text(settings['hosts'])
    Path('/etc/nsswitch.conf').write_text('hosts: files\npasswd: files\ngroup: files\n')
    uid=settings['uid'];Path('/etc/passwd').write_text(f'zk:x:{uid}:{uid}:ZooKeeper:/srv/zookeeper:/bin/false\n')
    Path('/etc/group').write_text(f'zk:x:{uid}:\n')
    Path('/etc/zookeeper').mkdir();command(['mount','--bind',settings['config'],'/etc/zookeeper']);command(['mount','-o','remount,bind,ro','/etc/zookeeper'])
    command(['mount','-t','tmpfs','-o','size=1m,mode=755','tmpfs','/srv'])
    state=Path(settings['data_dir']);state.mkdir(parents=True);command(['mount','--bind',settings['state'],str(state)])
    ready=Path(settings['ready']);ready.write_text(json.dumps(process(pid)))
    wait(lambda:Path(settings['gate']).is_file(),30)
    os.sched_setaffinity(0,set(settings['cpus']));os.setgroups([])
    libc=ctypes.CDLL(None,use_errno=True)
    for capability in range(64):libc.prctl(24,capability,0,0,0)
    os.setgid(uid);os.setuid(uid)
    if libc.prctl(38,1,0,0,0):raise OSError('NoNewPrivileges failed')
    os.chdir(state)
    sys.path.insert(0,'/etc/zookeeper');sys.modules.pop('start_zookeeper',None);module=importlib.import_module('start_zookeeper');assert Path(module.__file__)==Path('/etc/zookeeper/start_zookeeper.py')
    # Invoke the exact packaged launch guard; no configuration/trust overrides.
    module.launch(settings['member'],settings['digest'],initialize=settings['initialize'],home=Path(settings['home']))


def certificates(root):
    root.mkdir()
    def run(*args):command(['openssl',*args])
    for kind in ('client','quorum','untrusted'):
        run('req','-x509','-newkey','rsa:2048','-nodes','-days','2','-subj','/CN=fixture-'+kind,'-keyout',str(root/(kind+'-ca.key')),'-out',str(root/(kind+'-ca.pem')))
    def issue(name,ca,host,address,*,server=False):
        key=root/(name+'.key');cert=root/(name+'.pem');csr=root/(name+'.csr');ext=root/(name+'.ext')
        run('req','-new','-newkey','rsa:2048','-nodes','-subj','/CN='+name,'-keyout',str(key),'-out',str(csr))
        ext.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=clientAuth'+(',serverAuth' if server else '')+'\nsubjectAltName=DNS:'+host+',IP:'+address+'\n')
        run('x509','-req','-in',str(csr),'-CA',str(root/(ca+'-ca.pem')),'-CAkey',str(root/(ca+'-ca.key')),'-set_serial',str(int(uuid4().hex[:24],16)),'-days','2','-extfile',str(ext),'-out',str(cert))
        key.chmod(0o600);return cert,key
    for i in range(1,4):
        for ca in ('client','quorum'):issue(f'{ca}-{i}',ca,f'zk-{i}.example.com',f'192.0.2.{i+10}',server=True)
    for name in ('health','zuul','unrelated'):issue(name,'client',name+'.example.com','192.0.2.1')
    return root


def exercise(test, *, coordination=None):
    if coordination is not None and not callable(coordination):raise ValueError('Select an explicit coordination fixture callback')
    original=owner();assets=Path(os.environ['ZOOKEEPER_TEST_ARTIFACTS']).resolve();java_archive=Path(os.environ['ZOOKEEPER_TEST_JAVA_ARCHIVE']).resolve()
    for name,digest in PINS.items():pinned(assets/name,digest,40*1024*1024)
    pinned(java_archive,JAVA_SHA,100*1024*1024)
    report_path=Path(os.environ['ZOOKEEPER_TEST_REPORT']).resolve()
    project=Path(__file__).resolve().parents[1]
    if not report_path.is_relative_to(project/'private') or report_path.exists():raise ValueError('Require a fresh private report')
    sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (project/'scripts').glob('*.py')}
    report={'owner':original,'source_sha256':sources,'members':[],'events':[],'physical_ha_verified':False,'zuul_ci_verified':False,'full_stack_verified':False}
    clients=[];handles=[];networks={};extensions=contextlib.ExitStack();probe=None;stage='setup';failure=None
    with tempfile.TemporaryDirectory(prefix='platform-zookeeper-runtime-',dir='/var/tmp') as temporary:
        root=Path(temporary);root.chmod(0o755);report['scope']=str(root)
        try:
            with tarfile.open(java_archive) as archive:archive.extractall(root,filter='data')
            java=root/'jdk-21.0.13+9-jre';home=root/'distribution';(home/'lib').mkdir(parents=True)
            with tarfile.open(assets/'apache-zookeeper-3.9.7-bin.tar.gz') as archive:
                for item in archive.getmembers():
                    path=Path(item.name)
                    if path.parent==Path('apache-zookeeper-3.9.7-bin/lib') and path.suffix=='.jar':
                        if not item.isfile() or item.size>64*1024*1024:raise ValueError('Unexpected distribution jar')
                        (home/'lib'/path.name).write_bytes(archive.extractfile(item).read())
            wheel=root/'client';wheel.mkdir()
            with zipfile.ZipFile(assets/'kazoo-2.11.0-py2.py3-none-any.whl') as archive:
                for name in archive.namelist():
                    path=Path(name)
                    if path.is_absolute() or '..' in path.parts:raise ValueError('Invalid client wheel path')
                archive.extractall(wheel)
            sys.path.insert(0,str(wheel))
            from kazoo.client import KazooClient
            from kazoo.exceptions import (
                ConnectionLoss,
                NoAuthError,
                SessionExpiredError,
            )
            from kazoo.handlers.threading import KazooTimeoutError
            from kazoo.security import make_acl
            logging.getLogger('kazoo').setLevel(logging.CRITICAL)
            cert=certificates(root/'pki')
            hosts='127.0.0.1 localhost\n'+''.join(f'192.0.2.{i+10} zk-{i}.example.com\n' for i in range(1,4))
            if coordination is not None:hosts+='192.0.2.11 wrong-zk.example.com\n'
            hostsfile=root/'hosts';hostsfile.write_text(hosts);command(['mount','--make-rprivate','/']);command(['mount','--bind',str(hostsfile),'/etc/hosts']);command(['mount','-o','remount,bind,ro','/etc/hosts'])
            command(['ip','link','set','lo','up']);command(['ip','link','add','brzk','type','bridge']);command(['ip','addr','add','192.0.2.1/24','dev','brzk']);command(['ip','link','set','brzk','up'])
            test.assertEqual(json.loads(command(['ip','-j','route','show','default']).stdout),[])
            plan,ci,_=plans();plan.update(ensemble_id=str(uuid4()),zookeeper_version=VERSION,initialize_if_empty=True,artifacts={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (home/'lib').glob('*.jar')})
            rendered=renderer.render(plan,ci,'raw');digests=json.loads(rendered['render-status.json'])['contract_sha256'];report['plan']=plan;report['jar_sha256']=plan['artifacts']
            report['rendered_sha256']={name:hashlib.sha256(value.encode()).hexdigest() for name,value in rendered.items()}
            configs={};states={};contracts={}
            for member in plan['members']:
                name=member['name'];i=member['id'];uid=60100+i;cfg=root/name;cfg.mkdir(mode=0o755);private=cfg/'private';private.mkdir(mode=0o700);os.chown(private,uid,uid)
                for filename,value in rendered.items():(cfg/filename).write_text(value)
                for filename,content in {'client-server.pem':(cert/f'client-{i}.pem').read_bytes()+(cert/f'client-{i}.key').read_bytes(),'quorum-server.pem':(cert/f'quorum-{i}.pem').read_bytes()+(cert/f'quorum-{i}.key').read_bytes(),'client-ca.pem':(cert/'client-ca.pem').read_bytes(),'quorum-ca.pem':(cert/'quorum-ca.pem').read_bytes(),'health-client.pem':(cert/'health.pem').read_bytes(),'health-client.key':(cert/'health.key').read_bytes()}.items():
                    target=private/filename;target.write_bytes(content);target.chmod(0o600);os.chown(target,uid,uid)
                state=root/(name+'-state');state.mkdir(mode=0o700);os.chown(state,uid,uid);configs[name]=cfg;states[name]=state;contracts[name]=json.loads(rendered[name+'-contract.json'])
            def start(member,initialize):
                name=member['name'];i=member['id'];generation=sum(h['member']==name for h in handles);unit='platform-zookeeper-member-'+uuid4().hex+'.service'
                log=root/(unit+'.log');settings={'owner':original,'root':str(root),'member':name,'uid':60100+i,'config':str(configs[name]),'state':str(states[name]),'data_dir':member['state_host_path'],'digest':digests[name],'initialize':initialize,'home':str(home),'hosts':hosts,'cpus':sorted(os.sched_getaffinity(0))[:2],'ready':str(root/(unit+'.ready')),'gate':str(root/(unit+'.gate'))}
                if name in networks:settings['network_path']='/proc/'+str(os.getpid())+'/fd/'+str(networks[name])
                path=root/(unit+'.json');path.write_text(json.dumps(settings));path.chmod(0o600)
                runtime_limit='720' if any(os.environ.get(k)=='1' for k in ('ZUUL_EXECUTOR_RECOVERY_TEST_RUNTIME','ZUUL_INFLIGHT_RECOVERY_TEST_RUNTIME')) else '300'
                if os.environ.get('ZUUL_RKE2_SERVING_TEST_RUNTIME')=='1':runtime_limit='3500'
                if original['unit'].startswith('platform-k3s-vm-zuul-artifact-'):runtime_limit='1200'
                args=['systemd-run','--quiet','--collect','--service-type=exec','--unit='+unit,'--property=RuntimeMaxSec='+runtime_limit,'--property=MemoryMax=2G','--property=MemorySwapMax=0','--property=CPUQuota=200%','--property=TasksMax=256','--property=KillMode=control-group','--property=NoNewPrivileges=yes','--property=BindsTo='+original['unit'],'--property=After='+original['unit'],'--property=NetworkNamespacePath=/proc/'+str(os.getpid())+'/ns/net','--property=StandardOutput=append:'+str(log),'--property=StandardError=inherit','--setenv=PATH='+str(java/'bin')+':/usr/bin:/bin',sys.executable,'-B',str(Path(__file__).resolve()),'child',str(path)]
                command(args);info=show(unit);pid=int(info['MainPID']);handle={'member':name,'generation':generation,'unit':unit,'invocation_id':info['InvocationID'],'process':process(pid),'log_path':str(log),'removed':False};handles.append(handle);report['members'].append(handle)
                wait(lambda:Path(settings['ready']).exists(),20);test.assertEqual(pid,json.loads(Path(settings['ready']).read_bytes())['pid']);handle['process']=process(pid)
                if name not in networks:
                    networks[name]=os.open('/proc/'+str(pid)+'/ns/net',os.O_RDONLY)
                    left='zp'+str(i);right='zc'+str(i);command(['ip','link','add',left,'type','veth','peer','name',right]);command(['ip','link','set',right,'netns',str(pid)]);command(['ip','link','set',left,'master','brzk']);command(['ip','link','set',left,'up'])
                    def net(*args):command(['nsenter','-t',str(pid),'-n','ip',*args])
                    net('link','set','lo','up');net('addr','add',member['bind_address']+'/24','dev',right);net('link','set',right,'up')
                test.assertEqual(json.loads(command(['nsenter','-t',str(pid),'-n','ip','-j','route','show','default']).stdout),[])
                handle['network_inode']=Path('/proc',str(pid),'ns/net').stat().st_ino;test.assertEqual(handle['network_inode'],os.fstat(networks[name]).st_ino);Path(settings['gate']).touch();return handle
            def live(handle):
                info=show(handle['unit']);test.assertEqual(info.get('InvocationID'),handle['invocation_id']);test.assertEqual(info.get('MainPID'),str(handle['process']['pid']));test.assertEqual(info.get('MemoryMax'),str(2*1024**3));test.assertEqual(info.get('TasksMax'),'256');test.assertEqual(info.get('NRestarts'),'0')
                now=process(handle['process']['pid']);test.assertEqual(now['start_ticks'],handle['process']['start_ticks']);return now
            def modes(selected):
                found={}
                for member in selected:
                    try:found[member['name']]=starter.probe(contracts[member['name']],root=configs[member['name']])['mode']
                    except (OSError,ValueError,ssl.SSLError):return False
                return found if list(found.values()).count('leader')==1 and list(found.values()).count('follower')==len(selected)-1 else False
            def stop(handle):
                if handle['removed']:return
                info=show(handle['unit'])
                if info.get('LoadState')!='not-found':
                    test.assertEqual(info.get('InvocationID'),handle['invocation_id']);command(['systemctl','stop',handle['unit']],check=False)
                wait(lambda:absent(handle['process']),20);handle['removed']=True
            def client(identity,ordered):
                c=KazooClient(hosts=','.join(m['host']+':'+str(plan['client_port']) for m in ordered),timeout=30,use_ssl=True,verify_certs=True,check_hostname=True,certfile=str(cert/(identity+'.pem')),keyfile=str(cert/(identity+'.key')),ca=str(cert/'client-ca.pem'),randomize_hosts=False,connection_retry={'max_tries':10,'delay':.2,'max_delay':1},command_retry={'max_tries':1})
                clients.append(c);c.start(timeout=20);return c
            stage='three-member-start';print(json.dumps({'zookeeper_stage':stage}),flush=True)
            for member in plan['members']:start(member,True)
            serving=wait(lambda:modes(plan['members']));report['initial_modes']=serving
            for handle in handles:live(handle)
            test.assertEqual(len({h['network_inode'] for h in handles}),3)
            leader=next(m for m in plan['members'] if serving[m['name']]=='leader');others=[m for m in plan['members'] if m!=leader]
            zk=client('zuul',[leader,*others]);session=zk.client_id[0];report['original_session_id']=session
            acl=[make_acl('x509','CN=zuul',all=True)];path='/fixture-'+uuid4().hex;value=os.urandom(32)
            zk.create_async(path,value,acl=acl).get(timeout=10);ephemeral=path+'/session';zk.create_async(ephemeral,b'owned',acl=acl,ephemeral=True).get(timeout=10)
            test.assertEqual(zk.get(path)[0],value);test.assertEqual(zk.get(ephemeral)[1].ephemeralOwner,session)
            other=client('unrelated',plan['members'])
            for operation in (lambda:other.get(path),lambda:other.set(path,b'forbidden'),lambda:other.delete(ephemeral)):
                with test.assertRaises(NoAuthError):operation()
            report['x509_acl_read_write_delete_denied']=True
            # Each trust negative must fail the actual TLS path, not only parsing.
            for wrong_name,wrong_ca,no_certificate in ((True,False,False),(False,True,False),(False,False,True)):
                context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.load_verify_locations(str(cert/('untrusted-ca.pem' if wrong_ca else 'client-ca.pem')))
                if not no_certificate:context.load_cert_chain(str(cert/'health.pem'),str(cert/'health.key'))
                try:
                    with socket.create_connection((leader['bind_address'],plan['client_port']),timeout=3) as raw, context.wrap_socket(raw,server_hostname='wrong.example.com' if wrong_name else leader['host']) as wire:
                        wire.sendall(b'srvr');reply=wire.recv(16384)
                except ssl.SSLCertVerificationError:
                    test.assertTrue(wrong_name or wrong_ca)
                except (ssl.SSLError,ConnectionResetError):
                    test.assertTrue(no_certificate)
                else:
                    test.assertTrue(no_certificate);test.assertEqual(reply,b'')
                test.assertIn(starter.probe(contracts[leader['name']],root=configs[leader['name']])['mode'],('leader','follower'))
            report['tls_trust_hostname_and_client_certificate_negatives_verified']=True
            if coordination is not None:
                stage='actual-zuul-coordination';print(json.dumps({'zookeeper_stage':stage}),flush=True)
                probe=extensions.enter_context(coordination({'test':test,'root':root,'cert':cert,'plan':plan,'oracle':zk,'report':report}))
            stage='leader-loss';print(json.dumps({'zookeeper_stage':stage}),flush=True)
            victim=next(h for h in handles if h['member']==leader['name']);live(victim);command(['systemctl','kill','--kill-whom=all','--signal=KILL',victim['unit']]);wait(lambda:absent(victim['process']),15);victim['removed']=True
            # A daemon-loss test retains its network identity. Otherwise it also
            # blackholes TCP and tests a different fault plane.
            # MainPID exit can precede final Netty socket teardown. Require
            # actual port refusal within the original fault window, rather
            # than treating one in-flight SYN/ACK as a surviving daemon.
            def port_refused():
                try:
                    with socket.create_connection((leader['bind_address'],plan['client_port']),timeout=2):return False
                except ConnectionRefusedError:return True
            wait(port_refused,5)
            report['daemon_loss_network_retained_and_port_refused']=True
            failover=wait(lambda:modes(others));report['survivor_modes']=failover
            zk.create_async(path+'/after-leader-loss',b'acknowledged',acl=acl).get(timeout=20)
            test.assertEqual(zk.client_id[0],session);test.assertEqual(zk.get(ephemeral)[1].ephemeralOwner,session);report['same_client_session_after_leader_loss_verified']=True
            if probe is not None:probe.check('after-leader-loss')
            # Retain and re-use original member identity and state; never initialize it again.
            marker=read_bounded_bytes(states[leader['name']]/starter.MARKER);myid=read_bounded_bytes(states[leader['name']]/'myid')
            start(leader,False);report['recovered_modes']=wait(lambda:modes(plan['members']))
            test.assertEqual(read_bounded_bytes(states[leader['name']]/starter.MARKER),marker);test.assertEqual(read_bounded_bytes(states[leader['name']]/'myid'),myid)
            zk.sync_async(path).get(timeout=10);test.assertEqual(zk.get(path)[0],value);report['original_member_state_rejoin_verified']=True
            stage='quorum-loss';print(json.dumps({'zookeeper_stage':stage}),flush=True)
            paused=[h for h in handles if not h['removed']][:2];probe_started=time.monotonic()
            try:
                for handle in paused:
                    live(handle);command(['systemctl','kill','--kill-whom=all','--signal=STOP',handle['unit']])
                    status=Path('/proc',str(handle['process']['pid']),'status').read_text();test.assertRegex(status,r'(?m)^State:\s+T')
                pending=zk.create_async(path+'/no-quorum',b'pending',acl=acl)
                try:pending.get(timeout=8)
                except (KazooTimeoutError,ConnectionLoss,SessionExpiredError) as error:
                    report['quorum_loss_probe']={'paused_original_processes':[h['process'] for h in paused],'started_monotonic':probe_started,'unacknowledged_at_monotonic':time.monotonic(),'error_type':type(error).__name__,'acknowledged_while_quorum_absent':False}
                else:raise AssertionError('A write was acknowledged without two voters')
            finally:
                for handle in paused:
                    command(['systemctl','kill','--kill-whom=all','--signal=CONT',handle['unit']]);live(handle)
            report['quorum_loss_probe']['resumed_monotonic']=time.monotonic()
            report['final_modes']=wait(lambda:modes(plan['members']))
            zk.sync_async(path).get(timeout=15);test.assertEqual(zk.get(path)[0],value);test.assertEqual(zk.get_acls(path)[0],acl)
            # A timed-out request can commit after recovery. Preserve that ambiguity;
            # it must never become an acknowledged outage write or a zero-RPO claim.
            try:pending.get(timeout=10)
            except (KazooTimeoutError,ConnectionLoss,SessionExpiredError):report['quorum_loss_probe']['late_request_acknowledged']=False
            else:report['quorum_loss_probe']['late_request_acknowledged']=True
            with test.assertRaises(NoAuthError):other.get(path)
            report['minority_write_refused_and_original_quorum_recovered']=True
            stage='network-partition';print(json.dumps({'zookeeper_stage':stage}),flush=True)
            def peer():
                try:return zk._connection._socket.getpeername()[0] if zk.connected else None
                except (AttributeError,OSError):return None
            connected=wait(peer,20);partitioned=next(m for m in plan['members'] if m['bind_address']==connected)
            remaining=[m for m in plan['members'] if m!=partitioned];survivor_ips={m['bind_address'] for m in remaining}
            lease=path+'/network-session';zk.create_async(lease,b'lease',acl=acl,ephemeral=True).get(timeout=10);old_session=zk.client_id[0]
            before_partition={h['member']:live(h) for h in handles if not h['removed']};partition_started=time.monotonic()
            try:
                command(['ip','link','set','zp'+str(partitioned['id']),'down'])
                partition_modes=wait(lambda:modes(remaining),60)
                wait(lambda:peer() in survivor_ips,90)
                zk.create_async(path+'/after-partition',b'acknowledged',acl=acl).get(timeout=20)
                new_session=zk.client_id[0]
                if new_session==old_session:test.assertEqual(zk.get(lease)[1].ephemeralOwner,old_session)
                else:wait(lambda:zk.exists(lease) is None,40)
                report['network_partition']={'member':partitioned['name'],'original_processes':before_partition,'started_monotonic':partition_started,'write_acknowledged_monotonic':time.monotonic(),'survivor_modes':partition_modes,'connected_survivor_ip':peer(),'old_session_id':old_session,'new_session_id':new_session,'expired_session_ephemeral_fenced':new_session!=old_session,'same_session_retained':new_session==old_session}
            finally:command(['ip','link','set','zp'+str(partitioned['id']),'up'])
            report['network_partition']['resumed_monotonic']=time.monotonic();report['post_partition_modes']=wait(lambda:modes(plan['members']))
            for handle in handles:
                if not handle['removed']:test.assertEqual(live(handle),before_partition[handle['member']])
            test.assertEqual(zk.get(path)[0],value)
            with test.assertRaises(NoAuthError):other.get(path)
            report['network_partition_reconnect_and_session_fencing_verified']=True
            if probe is not None:probe.check('after-quorum-and-network-recovery')
            extensions.close()

            report['real_three_member_tls_quorum_verified']=True;report['artifact_sha256']={**PINS,'java_archive':JAVA_SHA}
            for handle in handles:
                if not handle['removed']:live(handle)
            stage='complete'
        except Exception as error:
            failure=error;report['failure']={'stage':stage,'type':type(error).__name__};raise
        finally:
            extensions.close()
            for c in reversed(clients):c.stop();c.close()
            for handle in reversed(handles):
                info=show(handle['unit'])
                if info.get('LoadState')!='not-found':
                    test.assertEqual(info.get('InvocationID'),handle['invocation_id']);command(['systemctl','stop',handle['unit']],check=False)
                wait(lambda handle=handle:absent(handle['process']),20);handle['removed']=True
                log=Path(handle['log_path']);handle['private_log']=read_bounded_bytes(log,max_bytes=1024*1024).decode(errors='replace') if log.exists() else ''
            for descriptor in networks.values():os.close(descriptor)
            report['source_inventory_unchanged']=sources=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (project/'scripts').glob('*.py')}
            report['original_member_processes_removed']=all(h['removed'] for h in handles)
            report_path.parent.mkdir(parents=True,exist_ok=True);descriptor=os.open(report_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'w') as output:json.dump(report,output,sort_keys=True);output.write('\n')
    test.assertFalse(root.exists());test.assertTrue(report['source_inventory_unchanged']);test.assertIsNone(failure)
    return report


if __name__=='__main__' and len(sys.argv)==3 and sys.argv[1]=='child':child(sys.argv[2])
