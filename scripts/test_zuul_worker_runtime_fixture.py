"""Opt-in loopback-only OpenSSH authentication fixture; never a production worker."""

import json
import contextlib
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest

import prepare_zuul_worker_fixture as artifacts
from test_zookeeper_runtime_fixture import absent, command, owner, process, wait


def child(settings_file):
    settings = json.loads(Path(settings_file).read_bytes())
    if Path('/proc/self/ns/net').stat().st_ino == Path('/proc/1/ns/net').stat().st_ino:
        raise ValueError('Worker must be in an isolated network namespace')
    os.unshare(os.CLONE_NEWNS)
    command(['mount', '--make-rprivate', '/'])
    command(['mount', '-t', 'tmpfs', '-o', 'size=4m,mode=755', 'tmpfs', '/etc'])
    Path('/etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\nworker:x:60601:60601:fixture:/tmp:/bin/sh\nsshd:x:60602:60602:sshd:/tmp:/bin/false\n')
    Path('/etc/group').write_text('root:x:0:\nworker:x:60601:\nsshd:x:60602:\n')
    Path('/etc/shadow').write_text('root:!:19000:0:99999:7:::\nworker::19000:0:99999:7:::\nsshd:!:19000:0:99999:7:::\n')
    Path('/etc/shadow').chmod(0o600)
    Path('/etc/nsswitch.conf').write_text('passwd: files\ngroup: files\nshadow: files\nhosts: files\n')
    Path('/etc/worker_authorized_keys').write_bytes(Path(settings['authorized']).read_bytes())
    Path('/etc/worker_authorized_keys').chmod(0o644)
    command(['mount', '-t', 'tmpfs', '-o', 'size=4m,mode=755', 'tmpfs', '/run'])
    Path('/run/sshd').mkdir(mode=0o755)
    command(['mount', '--bind', settings['openssh_lib'], '/usr/lib/openssh'])
    command(['mount', '-o', 'remount,bind,ro', '/usr/lib/openssh'])
    if settings.get('sandboxed'):
        command(['mount', '-t', 'tmpfs', '-o', 'size=128m,mode=755', 'tmpfs', '/srv'])
        jail=Path('/srv/jail');jail.mkdir(mode=0o755)
        (jail/'usr').mkdir();command(['mount','--bind','/usr',str(jail/'usr')])
        command(['mount','-o','remount,bind,ro',str(jail/'usr')])
        for name,target in (('bin','usr/bin'),('lib','usr/lib'),('lib64','usr/lib64')):
            (jail/name).symlink_to(target)
        (jail/'dev').mkdir();(jail/'tmp').mkdir(mode=0o1777);(jail/'tmp').chmod(0o1777)
        (jail/'etc').mkdir();(jail/'etc/passwd').write_text('worker:x:60601:60601:fixture:/tmp:/bin/sh\n')
        (jail/'etc/group').write_text('worker:x:60601:\n')
        for name in ('null','urandom','zero'):
            target=jail/'dev'/name;target.touch();command(['mount','--bind','/dev/'+name,str(target)])
        if settings.get('evidence'):
            (jail/'evidence').mkdir()
            command(['mount','--bind',settings['evidence'],str(jail/'evidence')])
    env = {'PATH':'/usr/bin:/bin', 'LANG':'C.UTF-8', 'LD_LIBRARY_PATH':settings['libdir']}
    os.execve(settings['sshd'], [settings['sshd'], '-D', '-e', '-f', settings['config']], env)


@contextlib.contextmanager
def job_worker(public_key, source, installed):
    """Launch a test-only chrooted SSH worker in the already isolated namespace."""
    owner()
    artifacts.verify(source, installed)
    with tempfile.TemporaryDirectory(prefix='platform-zuul-job-worker-', dir='/var/tmp') as directory:
        root=Path(directory);root.chmod(0o755)
        evidence=root/'evidence';evidence.mkdir(mode=0o700);os.chown(evidence,60601,60601)
        command(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(root/'host')])
        authorized=root/'authorized_keys';authorized.write_bytes(Path(public_key).read_bytes());authorized.chmod(0o644)
        os.chown(authorized,60601,60601)
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        host_key=' '.join((root/'host.pub').read_text().split()[:2])
        config=root/'sshd_config'
        config.write_text(f'Port {port}\nListenAddress 127.0.0.1\nHostKey {root / "host"}\n'
                          f'AuthorizedKeysFile /etc/worker_authorized_keys\nPidFile {root / "sshd.pid"}\n'
                          'UsePAM no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n'
                          'PermitRootLogin no\nAllowUsers worker\nStrictModes yes\n'
                          'AllowTcpForwarding no\nX11Forwarding no\nPermitTTY no\n'
                          'Subsystem sftp internal-sftp\nChrootDirectory /srv/jail\nLogLevel ERROR\n')
        settings={'sshd':str(installed/'usr/sbin/sshd'),'config':str(config),
                  'openssh_lib':str(installed/'usr/lib/openssh'),'authorized':str(authorized),
                  'libdir':str(installed/'usr/lib/x86_64-linux-gnu'),'sandboxed':True,'evidence':str(evidence)}
        settings_file=root/'settings.json';settings_file.write_text(json.dumps(settings))
        with (root/'sshd.log').open('wb') as log:
            server=subprocess.Popen([sys.executable,'-B',__file__,'child',str(settings_file)],
                                    stdout=log,stderr=log,start_new_session=True)
            original=process(server.pid)
            try:
                def ready():
                    if server.poll() is not None:raise RuntimeError('Disposable job worker stopped during startup')
                    try:
                        with socket.create_connection(('127.0.0.1',port),timeout=1):return True
                    except OSError:return False
                wait(ready,20)
                yield {'host':'127.0.0.1','port':port,'host_key':host_key,'username':'worker','evidence':evidence}
            finally:
                if not absent(original):os.killpg(server.pid,signal.SIGTERM)
                server.wait(timeout=15)
                if not absent(original):raise RuntimeError('Disposable job worker remained running')


@unittest.skipUnless(os.environ.get('ZUUL_WORKER_RUNTIME_TEST') == '1', 'explicit isolated worker runtime required')
class WorkerRuntimeTests(unittest.TestCase):
    def test_disposable_worker_authenticates_only_the_generated_key(self):
        owner()
        source = Path(os.environ['ZUUL_WORKER_PACKAGE_DIR'])
        installed = Path(os.environ['ZUUL_WORKER_EXTRACTED'])
        artifacts.verify(source, installed)
        sandboxed=os.environ.get('ZUUL_WORKER_JAIL_TEST')=='1'
        command(['ip', 'link', 'set', 'lo', 'up'])
        links = command(['ip', '-o', 'link', 'show']).stdout.splitlines()
        self.assertEqual([line.split(':', 2)[1].strip() for line in links], ['lo'])
        self.assertFalse(command(['ip', '-4', 'route', 'show']).stdout.strip())
        with tempfile.TemporaryDirectory(prefix='platform-zuul-worker-', dir='/var/tmp') as directory:
            root = Path(directory)
            root.chmod(0o755)
            for name in ('host', 'good', 'bad'):
                command(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(root / name)])
            authorized = root / 'authorized_keys'
            authorized.write_bytes((root / 'good.pub').read_bytes())
            authorized.chmod(0o644)
            os.chown(authorized, 60601, 60601)
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            sock.close()
            host_public = ' '.join((root / 'host.pub').read_text().split()[:2])
            (root / 'known_hosts').write_text(f'[127.0.0.1]:{port} {host_public}\n')
            config = root / 'sshd_config'
            config.write_text(f'Port {port}\nListenAddress 127.0.0.1\nHostKey {root / "host"}\n'
                              f'AuthorizedKeysFile /etc/worker_authorized_keys\nPidFile {root / "sshd.pid"}\n'
                              'UsePAM no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n'
                              'PermitRootLogin no\nAllowUsers worker\nStrictModes yes\n'
                              'AllowTcpForwarding no\nX11Forwarding no\nPermitTTY no\n'
                              'Subsystem sftp internal-sftp\n'
                              + ('ChrootDirectory /srv/jail\n' if sandboxed else 'ForceCommand /usr/bin/id -u\n')
                              + 'LogLevel VERBOSE\n')
            settings = {'sshd': str(installed / 'usr/sbin/sshd'), 'config': str(config),
                        'openssh_lib': str(installed / 'usr/lib/openssh'), 'authorized': str(authorized),
                        'libdir': str(installed / 'usr/lib/x86_64-linux-gnu'), 'sandboxed':sandboxed}
            settings_file = root / 'settings.json'
            settings_file.write_text(json.dumps(settings))
            log = (root / 'sshd.log').open('wb')
            server = subprocess.Popen([sys.executable, '-B', __file__, 'child', str(settings_file)],
                                      stdout=log, stderr=log, start_new_session=True)
            original = process(server.pid)
            try:
                def ready():
                    if server.poll() is not None:
                        raise RuntimeError('Disposable SSH server stopped during startup')
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=1):
                            return True
                    except OSError:
                        return False
                wait(ready, 20)
                base = ['ssh', '-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                        '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={root / "known_hosts"}',
                        '-o', 'ConnectTimeout=5', '-p', str(port)]
                remote = ("python3 -c 'import os; print(os.getuid()); print(os.path.exists(\"/var/tmp\")); "
                          "print(os.path.exists(\"/mnt\")); print(os.path.exists(\"/proc\")); "
                          "print(bool(os.statvfs(\"/usr\").f_flag & os.ST_RDONLY)); "
                          "p=\"/tmp/probe\"; open(p,\"w\").write(\"ok\"); os.unlink(p); print(\"tmp-ok\")'"
                          if sandboxed else 'ignored')
                good = command([*base, '-i', str(root / 'good'), 'worker@127.0.0.1', remote], check=False)
                if good.returncode:
                    log.flush()
                    diagnostics = (root / 'sshd.log').read_text(errors='replace')
                    raise AssertionError('SSH fixture failed: ' + good.stderr[-1000:] + ' | ' + diagnostics[-1500:])
                self.assertEqual(good.stdout.strip(), '60601\nFalse\nFalse\nFalse\nTrue\ntmp-ok' if sandboxed else '60601')
                if sandboxed:
                    payload=root/'sftp-fixture';payload.write_text('fixture-only\n')
                    transfer=command(['sftp','-F','/dev/null','-b','-','-P',str(port),
                                      '-o','BatchMode=yes','-o','IdentitiesOnly=yes',
                                      '-o','StrictHostKeyChecking=yes','-o',f'UserKnownHostsFile={root / "known_hosts"}',
                                      '-i',str(root/'good'),'worker@127.0.0.1'],
                                     input=f'put {payload} /tmp/sftp-fixture\nrm /tmp/sftp-fixture\n',check=False)
                    self.assertEqual(transfer.returncode,0)
                    ansible=os.environ.get('ZUUL_WORKER_ANSIBLE')
                    if ansible:
                        (root/'ansible.cfg').write_text('[defaults]\n')
                        options={'ansible_port':port,'ansible_python_interpreter':'/usr/bin/python3',
                                 'ansible_ssh_common_args':f'-o UserKnownHostsFile={root / "known_hosts"} -o StrictHostKeyChecking=yes'}
                        ansible_result=command([ansible,'all','-i','127.0.0.1,','-u','worker','--private-key',str(root/'good'),
                                                '-e',json.dumps(options),'-m','ansible.builtin.command','-a','/usr/bin/id -u'],
                                               env={'PATH':'/usr/bin:/bin','HOME':str(root),'LANG':'C.UTF-8',
                                                    'ANSIBLE_CONFIG':str(root/'ansible.cfg'),'PYTHONDONTWRITEBYTECODE':'1'},check=False)
                        self.assertEqual(ansible_result.returncode,0,ansible_result.stdout[-1500:]+ansible_result.stderr[-1500:])
                        self.assertIn('60601',ansible_result.stdout)
                bad = command([*base, '-i', str(root / 'bad'), 'worker@127.0.0.1', 'ignored'], check=False)
                self.assertNotEqual(bad.returncode, 0)
                wrong_host = ' '.join((root / 'bad.pub').read_text().split()[:2])
                (root / 'known_hosts').write_text(f'[127.0.0.1]:{port} {wrong_host}\n')
                wrong_pin = command([*base, '-i', str(root / 'good'), 'worker@127.0.0.1', 'ignored'], check=False)
                self.assertNotEqual(wrong_pin.returncode, 0)
            finally:
                if not absent(original):
                    os.killpg(server.pid, signal.SIGTERM)
                server.wait(timeout=15)
                log.close()
            self.assertTrue(absent(original))


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == 'child':
    child(sys.argv[2])
