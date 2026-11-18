#!/usr/bin/env python3
"""Bounded original-container log streams for admission fault evidence."""

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from bounded_subprocess import run_bounded


def previous_logs(client,pod,container,private_values):
    result=run_bounded([*client.prefix,'--request-timeout=15s','logs',pod['metadata']['name'],'-n',pod['metadata']['namespace'],
        '-c',container,'--previous','--tail=120'],text=True,timeout=30,check=False,output_max_bytes=1024*1024)
    if any(value in (result.stdout+result.stderr).encode() for value in private_values):raise ValueError('Private credential in container log diagnostics')
    return {'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr}


def select_previous_logs(api_read,witness):
    # A follow stream may end successfully when the paused kubelet closes the
    # connection, before its original container writes its shutdown records.
    # Prefer a successful previous-container read; the attached original stream
    # is a fallback only when that read is unavailable, never an override.
    if api_read is not None and api_read.get('returncode')==0:
        return {'previous_logs':api_read['stdout'],'previous_logs_source':'previous-container-api'}
    if witness is not None and witness.get('returncode')==0 and witness.get('capture_error') is None:
        return {'previous_logs':witness['stdout'],'previous_logs_source':'original-follow-stream','original_log_witness':witness}
    return {}


class BoundedLogStream:
    def __init__(self,arguments,private_values,*,seconds=300,max_bytes=1024*1024):
        if not 0<seconds<=300 or type(max_bytes) is not int or not 0<max_bytes<=1024*1024:raise ValueError('Require bounded original log capture')
        self.values=private_values;self.maximum=max_bytes;self.output={'stdout':bytearray(),'stderr':bytearray()};self.lock=threading.Lock();self.error=None
        self.started=time.monotonic();self.deadline=self.started+seconds;self.done=threading.Event();self.first_stdout=threading.Event()
        self.child=subprocess.Popen(arguments,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        proc=Path('/proc')/str(self.child.pid)
        self.identity={'pid':self.child.pid,'start_ticks':int((proc/'stat').read_text().rsplit(') ',1)[1].split()[19]),
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'cgroup':(proc/'cgroup').read_text().strip()}
        self.readers=[]
        for name in ('stdout','stderr'):
            thread=threading.Thread(target=self.read,args=(name,getattr(self.child,name)),daemon=True);thread.start();self.readers.append(thread)
        self.timer=threading.Thread(target=self.watch,daemon=True);self.timer.start()

    def terminate(self):
        if self.child.poll() is None:
            try:os.killpg(self.child.pid,signal.SIGTERM)
            except ProcessLookupError:pass

    def read(self,name,stream):
        try:
            while True:
                data=stream.read1(64*1024)
                if not data:break
                with self.lock:
                    if sum(len(v) for v in self.output.values())+len(data)>self.maximum:
                        self.error='output_limit';self.terminate();break
                    self.output[name].extend(data)
                    if name=='stdout':self.first_stdout.set()
        except (OSError,ValueError) as error:
            self.error=type(error).__name__;self.terminate()
        finally:stream.close()

    def watch(self):
        while not self.done.wait(0.1):
            if self.child.poll() is not None:return
            if time.monotonic()>=self.deadline:
                self.error='capture_deadline';self.terminate();return

    def snapshot(self):
        with self.lock:values={k:bytes(v) for k,v in self.output.items()}
        if any(value in values['stdout']+values['stderr'] for value in self.values):raise ValueError('Private credential in original container log stream')
        return {'original_process':dict(self.identity),'started_monotonic':self.started,'returncode':self.child.poll(),'capture_error':self.error,
            **{k:v.decode('utf-8',errors='replace') for k,v in values.items()}}

    def close(self):
        self.done.set();self.terminate()
        try:self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:os.killpg(self.child.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            self.child.wait(timeout=5)
        for thread in self.readers:thread.join(timeout=2)
        self.timer.join(timeout=2)
        if any(t.is_alive() for t in self.readers) or self.timer.is_alive():raise ValueError('Original log collector threads did not terminate')
        proc=Path('/proc')/str(self.child.pid)
        if proc.exists() and int((proc/'stat').read_text().rsplit(') ',1)[1].split()[19])==self.identity['start_ticks']:raise ValueError('Original log process remains')
        result=self.snapshot();result['original_process_removed']=True
        return result


class OriginalLogPool:
    def __init__(self,test,context,private_values):self.test=test;self.context=context;self.values=private_values;self.entries=[];self.closed=[]
    def start(self,pod,status,epoch):
        client=self.context['client'];before=client.get('pod/'+pod['metadata']['name'],pod['metadata']['namespace'])
        self.test.assertEqual(before['metadata']['uid'],pod['metadata']['uid'])
        current=next(c for c in before['status']['containerStatuses'] if c['name']==status['name'])
        self.test.assertEqual(current['containerID'],status['containerID']);self.test.assertEqual(current['restartCount'],status['restartCount'])
        stream=BoundedLogStream([*client.prefix,'--request-timeout=0s','logs',pod['metadata']['name'],'-n',pod['metadata']['namespace'],
            '-c',status['name'],'--follow','--timestamps','--tail=120'],self.values)
        entry={'pod_uid':pod['metadata']['uid'],'pod_name':pod['metadata']['name'],'namespace':pod['metadata']['namespace'],
            'container':status['name'],'container_id':status['containerID'],'restart_count':status['restartCount'],'epoch':epoch,'stream':stream}
        self.entries.append(entry)
        self.context['wait'](lambda:stream.first_stdout.is_set() and stream.child.poll() is None,seconds=15)
        after=client.get('pod/'+pod['metadata']['name'],pod['metadata']['namespace'])
        self.test.assertEqual(after['metadata']['uid'],pod['metadata']['uid'])
        active=next(c for c in after['status']['containerStatuses'] if c['name']==status['name'])
        self.test.assertEqual(active['containerID'],status['containerID']);self.test.assertEqual(active['restartCount'],status['restartCount'])
        entry.update(attached_monotonic=time.monotonic(),attachment_pod_before=before,attachment_pod_after=after)
        return entry

    def witness(self,pod_uid,container_id,*,wait=False):
        entries=[v for v in self.entries if v['pod_uid']==pod_uid and v['container_id']==container_id and 'attached_monotonic' in v]
        if not entries:return None
        entry=entries[-1];stream=entry['stream']
        if wait and stream.child.poll() is None:
            try:stream.child.wait(timeout=5)
            except subprocess.TimeoutExpired:return None
        if stream.child.poll()==0:
            for reader in stream.readers:reader.join(timeout=2)
            if any(reader.is_alive() for reader in stream.readers):return None
        # Only the completed, successfully read original stream can supply a
        # missing previous-container log. A live/new/failed stream is insufficient.
        result=stream.snapshot()
        if result['returncode']!=0 or result['capture_error'] is not None:return None
        return {**{k:v for k,v in entry.items() if k!='stream'},**result}

    def close(self):
        errors=[]
        for entry in reversed(self.entries):
            try:self.closed.append({**{k:v for k,v in entry.items() if k!='stream'},**entry['stream'].close()})
            except (OSError,ValueError,RuntimeError) as error:errors.append(type(error).__name__)
        if errors:raise ValueError('Some original log witnesses did not close')
        return self.closed
