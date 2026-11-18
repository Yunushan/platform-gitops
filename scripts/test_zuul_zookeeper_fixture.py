#!/usr/bin/env python3
"""Opt-in real Zuul coordination against the owned TLS ZooKeeper ensemble."""

import configparser
import contextlib
import hashlib
import json
import logging
import os
import selectors
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from bounded_file import read_bounded_bytes
from test_zookeeper_runtime_fixture import absent, process, wait

CANDIDATE_LOCK='7102acd0eb397ebcfdf65c8863eb38cb8f02f55bc1c43b5a551c69a6f1d344da'
ORIGINAL_LOCK='b637f3963c8091875d46bff828ed4be43f40886163207f91e0e003337f4b8d37'


def actor(settings_path):
    settings=json.loads(Path(settings_path).read_bytes());folder=Path(settings_path).parent
    import start_zuul
    from zuul.zk import ZooKeeperClient
    from zuul.zk.components import SchedulerComponent
    from zuul.zk.locks import SessionAwareLock
    config=configparser.RawConfigParser();config.read(folder/'zuul.conf')
    if settings['case']=='positive':
        start_zuul.verify_zookeeper_tls_config(folder)
        start_zuul.verify_zookeeper_client_policy('14.3.0')
    client=ZooKeeperClient.fromConfig(config)
    evidence={'process':process(os.getpid()),'euid':os.geteuid(),'status':Path('/proc/self/status').read_text()}
    if settings['case']!='positive':
        event=threading.Event();failures=[]
        class Capture(logging.Handler):
            def emit(self,record):
                pending=[*record.args] if isinstance(record.args,tuple) else []
                if record.exc_info:pending.append(record.exc_info[1])
                seen=set()
                while pending:
                    error=pending.pop()
                    if not isinstance(error,BaseException) or id(error) in seen:continue
                    seen.add(id(error));pending.extend([error.__cause__,error.__context__])
                    if isinstance(error,ssl.SSLCertVerificationError):
                        failures.append(error.verify_code);event.set()
        logging.getLogger('kazoo').addHandler(Capture())
        def connect():
            try:client.connect()
            except (OSError,ValueError) as error:logging.getLogger('fixture').error('Terminal TLS connection failure',exc_info=error)
        threading.Thread(target=connect,daemon=True).start()
        deadline=time.monotonic()+15
        while time.monotonic()<deadline and not event.is_set() and not client.connected:time.sleep(.05)
        evidence.update(connected=bool(client.connected),certificate_verification_codes=failures)
        print(json.dumps(evidence),flush=True)
        # Negative connect() deliberately retries forever upstream. This bounded
        # disposable process terminates only after capturing actual transport
        # verification evidence (or reports failure for the parent to reject).
        os._exit(0)
    client.connect();component=SchedulerComponent(client,settings['identity'],version='14.3.0');component.register();component.state=component.RUNNING
    lock=None;old_lock=None
    def snapshot():
        session=client.client.client_id
        return {'session_id':session[0] if session else None,'connected':bool(client.connected),'component_path':component.path,'lock_acquired':bool(lock and lock.is_acquired),'lock_valid':bool(lock and lock.is_still_valid()),'old_lock_valid':bool(old_lock and old_lock.is_still_valid())}
    print(json.dumps({**evidence,**snapshot()}),flush=True)
    try:
        for line in sys.stdin:
            if len(line)>1024:raise ValueError('Oversized fixture command')
            op=json.loads(line)['op']
            if op=='status':answer=snapshot()
            elif op=='checkpoint':
                component.state=component.PAUSED if component.state==component.RUNNING else component.RUNNING
                data,stat=client.client.get(component.path)
                answer={**snapshot(),'written_state':json.loads(data)['state'],'acknowledged_zxid':stat.mzxid}
            elif op=='acquire':
                if lock is None:lock=SessionAwareLock(client.client,settings['lock_path'],identifier=settings['identity'])
                answer={'acquired':lock.acquire(blocking=False),**snapshot()}
            elif op=='new-lock':
                old_lock=lock;lock=SessionAwareLock(client.client,settings['lock_path'],identifier=settings['identity']);answer=snapshot()
            elif op=='release':answer={'released':lock.release(),**snapshot()}
            elif op=='session-secret':
                # Private pipe to the independent fixture oracle; never included
                # in reports, public source, logs or command-line arguments.
                session,password=client.client.client_id;answer={'session_id':session,'password_hex':password.hex()}
            elif op=='stop':break
            else:raise ValueError('Unknown fixture command')
            print(json.dumps(answer),flush=True)
    finally:
        component.stop();client.disconnect()


def checked_environment(path,expected):
    path=path.resolve(strict=True);payload=read_bounded_bytes(path/'runtime-lock.json',max_bytes=8*1024**2)
    if hashlib.sha256(payload).hexdigest()!=expected:raise ValueError('Wrong reviewed SDK lock')
    lock=json.loads(payload)
    if hashlib.sha256(Path(lock['python_executable']).read_bytes()).hexdigest()!=lock['python_sha256']:raise ValueError('Reviewed interpreter changed')
    actual={str(p.relative_to(path)) for p in (path/'venv').rglob('*') if p.is_file() and not p.is_symlink()}
    if actual!=set(lock['files_sha256']):raise ValueError('Reviewed SDK file closure changed')
    for name,digest in lock['files_sha256'].items():
        file=path/name
        if file.is_symlink() or not file.resolve(strict=True).is_relative_to(path) or hashlib.sha256(read_bounded_bytes(file,max_bytes=128*1024**2)).hexdigest()!=digest:raise ValueError('Reviewed SDK source changed')
    return path


class Peer:
    def __init__(self,context,environment,identity,case='positive',*,wrong_ca=False,wrong_host=False):
        root=context['root'];cert=context['cert'];self.test=context['test'];self.identity=identity
        self.folder=root/('zuul-'+uuid4().hex);self.folder.mkdir(mode=0o700);os.chown(self.folder,65534,65534)
        private=self.folder/'private';private.mkdir(mode=0o700);os.chown(private,65534,65534)
        for target,source in (('zk-client.pem','zuul.pem'),('zk-client.key','zuul.key'),('zk-ca.pem','untrusted-ca.pem' if wrong_ca else 'client-ca.pem')):
            file=private/target;file.write_bytes((cert/source).read_bytes());file.chmod(0o600);os.chown(file,65534,65534)
        hosts=context['plan']['members'];port=context['plan']['client_port']
        # A fixture-only alias resolves to the same server but is absent from
        # that server's certificate SANs.
        addresses=','.join(('wrong-zk.example.com' if wrong_host else m['host'])+':'+str(port) for m in (hosts[:1] if wrong_host else hosts))
        config='[zookeeper]\nhosts='+addresses+'\nsession_timeout=30\n'+''.join(key+' = '+str(private/name)+'\n' for key,name in (('tls_cert','zk-client.pem'),('tls_key','zk-client.key'),('tls_ca','zk-ca.pem')))
        path=self.folder/'zuul.conf';path.write_text(config);path.chmod(0o600);os.chown(path,65534,65534)
        settings={'case':case,'identity':identity,'lock_path':context['lock_path']};path=self.folder/'input.json';path.write_text(json.dumps(settings));path.chmod(0o600);os.chown(path,65534,65534)
        self.log=self.folder/'stderr.log';self.stream=self.log.open('w');self.log.chmod(0o600)
        helper=Path(__file__).with_name('test_rook_api_fixture.py')
        args=[sys.executable,'-B',str(helper),'--child',str(os.getpid()),str(environment/'venv/bin/python'),'-B',str(Path(__file__).resolve()),'actor',str(path)]
        self.handle=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.stream,cwd=self.folder,env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'})
        self.original=process(self.handle.pid);self.first=None
    def initialize(self):
        self.first=self.read(30)
        self.test.assertEqual(self.first['process']['pid'],self.original['pid']);self.test.assertEqual(self.first['euid'],65534)
        self.test.assertRegex(self.first['status'],r'(?m)^NoNewPrivs:\s+1$');self.test.assertRegex(self.first['status'],r'(?m)^CapEff:\s+0+$')
    def read(self,seconds=20):
        with selectors.DefaultSelector() as selector:
            selector.register(self.handle.stdout,selectors.EVENT_READ)
            if not selector.select(seconds):raise TimeoutError('Original real Zuul peer response deadline')
        line=self.handle.stdout.readline(16384)
        if not line or not line.endswith(b'\n'):raise ValueError('Real Zuul peer exited without bounded response')
        return json.loads(line)
    def ask(self,op):
        self.test.assertEqual(self.handle.poll(),None);self.test.assertEqual(process(self.handle.pid),self.original)
        self.handle.stdin.write((json.dumps({'op':op})+'\n').encode());self.handle.stdin.flush();return self.read()
    def close(self):
        if self.handle.poll() is None:
            try:self.handle.stdin.write(b'{"op":"stop"}\n');self.handle.stdin.flush();self.handle.wait(timeout=15)
            except (BrokenPipeError,subprocess.TimeoutExpired):self.handle.kill();self.handle.wait(timeout=10)
        self.stream.close();self.test.assertTrue(absent(self.original))
        if hasattr(self,'record'):self.record['private_log']=read_bounded_bytes(self.log,max_bytes=1024*1024).decode(errors='replace')
        for stream in (self.handle.stdin,self.handle.stdout):stream.close()


@contextlib.contextmanager
def coordination(context):
    """Actual SDK objects; the generic Kazoo oracle remains a separate process."""
    test=context['test'];report=context['report'];peers=[]
    candidate=checked_environment(Path(os.environ['ZUUL_ZOOKEEPER_TEST_ENVIRONMENT']),CANDIDATE_LOCK)
    original=checked_environment(Path(os.environ['ZUUL_ZOOKEEPER_TEST_ORIGINAL']),ORIGINAL_LOCK)
    context['lock_path']='/zuul/fixture-locks/'+uuid4().hex
    details={'candidate_lock_sha256':CANDIDATE_LOCK,'original_lock_sha256':ORIGINAL_LOCK,'peers':[],'scheduler_execution_verified':False,'production_acl_verified':False}
    report['actual_zuul_coordination']=details
    def peer(identity,environment=candidate,**kwargs):
        item=Peer(context,environment,identity,**kwargs);peers.append(item)
        record={'identity':identity,'process':item.original};details['peers'].append(record);item.record=record
        item.initialize();record['first']={k:v for k,v in item.first.items() if k!='status'};return item
    try:
        # Compare the same real server and alias using separate immutable SDKs.
        baseline=peer('original-hostname',original,case='negative',wrong_host=True)
        test.assertTrue(baseline.first['connected']);test.assertEqual(baseline.first['certificate_verification_codes'],[]);baseline.close()
        for label,options,code in (('candidate-hostname',{'wrong_host':True},62),('candidate-ca',{'wrong_ca':True},20)):
            negative=peer(label,case='negative',**options);test.assertFalse(negative.first['connected']);test.assertIn(code,negative.first['certificate_verification_codes']);negative.close()
        details['actual_original_hostname_gap_and_candidate_tls_refusals_verified']=True
        first=peer('scheduler-a');second=peer('scheduler-b');oracle=context['oracle'];old=first.first
        for item in (first,second):
            data,stat=oracle.get(item.first['component_path']);test.assertEqual(stat.ephemeralOwner,item.first['session_id']);test.assertEqual(json.loads(data)['state'],'running')
        def lock_owner(item):
            children=oracle.get_children(context['lock_path']);test.assertEqual(len(children),1)
            data,stat=oracle.get(context['lock_path']+'/'+children[0]);test.assertEqual(data.decode(),item.identity)
            test.assertEqual(stat.ephemeralOwner,item.ask('status')['session_id'])
        test.assertTrue(first.ask('acquire')['acquired']);test.assertFalse(second.ask('acquire')['acquired']);lock_owner(first)
        details['actual_components_and_exclusive_lock_verified']=True
        # Close the original session through the real ZooKeeper protocol using
        # its private credentials; no LOST listener or exception is injected.
        secret=first.ask('session-secret')
        from kazoo.client import KazooClient
        thief=KazooClient(hosts=','.join(m['host']+':'+str(context['plan']['client_port']) for m in context['plan']['members']),timeout=10,use_ssl=True,verify_certs=True,check_hostname=True,certfile=str(context['cert']/'zuul.pem'),keyfile=str(context['cert']/'zuul.key'),ca=str(context['cert']/'client-ca.pem'),client_id=(secret['session_id'],bytes.fromhex(secret['password_hex'])))
        try:thief.start(timeout=10);thief.stop()
        finally:thief.close();del secret
        wait(lambda:oracle.exists(old['component_path']) is None,30)
        wait(lambda:second.ask('acquire')['acquired'],30);lock_owner(second)
        def renewed():
            status=first.ask('status')
            return status if status['connected'] and status['session_id'] is not None and status['session_id']!=old['session_id'] and status['component_path']!=old['component_path'] and not status['lock_valid'] else False
        fresh=wait(renewed,30);data,stat=oracle.get(fresh['component_path']);test.assertEqual(stat.ephemeralOwner,fresh['session_id'])
        first.ask('new-lock');test.assertFalse(first.ask('acquire')['acquired']);test.assertFalse(first.ask('status')['old_lock_valid'])
        second.ask('release');test.assertTrue(first.ask('acquire')['acquired']);lock_owner(first)
        details['real_protocol_expired_session_and_stale_lock_fencing_verified']=True
        details['expired_session_id']=old['session_id'];details['renewed_session_id']=fresh['session_id']
        class Probe:
            def check(self,phase):
                for item in (first,second):
                    status=item.ask('checkpoint')
                    data,stat=oracle.get(status['component_path']);test.assertEqual(stat.ephemeralOwner,status['session_id']);test.assertEqual(json.loads(data)['hostname'],item.identity)
                    test.assertEqual(json.loads(data)['state'],status['written_state']);test.assertEqual(stat.mzxid,status['acknowledged_zxid'])
                holder=first.ask('status');test.assertTrue(holder['lock_valid']);test.assertFalse(second.ask('acquire')['acquired']);lock_owner(first)
                details.setdefault('phases',[]).append({'phase':phase,'holder_session_id':holder['session_id'],'original_peer_processes_retained':True,'fresh_component_writes_independently_verified':True})
        probe=Probe();probe.check('initial');yield probe
        details['real_zuul_component_and_lock_lifecycle_verified']=len(details.get('phases',[]))==3
    finally:
        for item in reversed(peers):item.close()
        details['original_peer_processes_removed']=all(absent(p.original) for p in peers)


if __name__=='__main__' and len(sys.argv)==3 and sys.argv[1]=='actor':actor(sys.argv[2])
