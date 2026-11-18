#!/usr/bin/env python3
"""Explicit guarded Zuul launch; input-only checks remain read-only by default."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import importlib.metadata
import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from zuul_runtime_preflight import ROLES, verify_inputs


class ZuulLaunchError(ValueError):
    pass


def check_version(binary: str, expected: str) -> None:
    if not re.fullmatch(r"[1-9][0-9]*\.[0-9]+\.[0-9]+", expected):
        raise ZuulLaunchError("An exact stable release is required")
    result = run_bounded([binary, "--version"], timeout=20, text=True, input="", output_max_bytes=4096, check=False)
    versions = set(re.findall(r"(?<![\w.+-])[0-9]+\.[0-9]+\.[0-9]+(?![\w.+-])", result.stdout))
    if result.returncode or versions != {expected}:
        raise ZuulLaunchError("Installed daemon version does not match the reviewed release")


def stage_known_hosts(source: Path, state: Path) -> None:
    directory = state / ".ssh"
    if directory.is_symlink():
        raise ZuulLaunchError("SSH state must not be a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    metadata = directory.stat()
    if os.name == "posix" and metadata.st_mode & 0o022:
        raise ZuulLaunchError("SSH state must not be writable by other identities")
    destination = directory / "known_hosts"
    payload = read_bounded_bytes(source, max_bytes=4 * 1024 ** 2)
    if not payload:
        raise ZuulLaunchError("Provision a nonempty approved SSH trust file")
    if destination.is_symlink():
        raise ZuulLaunchError("Known-host trust file must not be a symlink")
    if destination.exists():
        metadata = destination.stat()
        if not stat.S_ISREG(metadata.st_mode) or (os.name == "posix" and metadata.st_mode & 0o022) or read_bounded_bytes(destination) != payload:
            raise ZuulLaunchError("Existing SSH trust differs or is unsafe; rotate it through private provisioning")
        return
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)


def gerrit_ssh_endpoints(config: Path) -> list[str]:
    """Resolve the same SSH host/port selectors as the Gerrit driver, offline."""
    parser=configparser.RawConfigParser(strict=True)
    try:
        parser.read_string(read_bounded_bytes(config,max_bytes=4*1024**2).decode('utf-8'))
        endpoints=[]
        for section in parser.sections():
            if not section.startswith('connection "') or not section.endswith('"') or parser.get(section,'driver',fallback='')!='gerrit':continue
            host=parser.get(section,'ssh_server',fallback=parser.get(section,'server',fallback=''))
            try:ipaddress.ip_address(host)
            except ValueError:
                if not isinstance(host,str) or len(host)>253 or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:[.][A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*',host):raise ZuulLaunchError('Declare an explicit Gerrit SSH endpoint') from None
            port=parser.get(section,'port',fallback='29418')
            if not re.fullmatch(r'[1-9][0-9]{0,4}',port) or not 1<=int(port)<=65535:raise ZuulLaunchError('Declare an explicit Gerrit SSH port')
            endpoints.append(host if int(port)==22 else '['+host+']:'+str(int(port)))
        if not endpoints or len(endpoints)>64:raise ZuulLaunchError('Require bounded configured Gerrit SSH endpoints')
        return sorted(set(endpoints))
    except (configparser.Error,UnicodeError) as error:
        raise ZuulLaunchError('Gerrit SSH endpoint configuration is invalid') from error


def verify_gerrit_host_pins(config: Path, source: Path) -> None:
    """Unknown-key acceptance in upstream is unreachable for these endpoints.

    Use its actual parser and lookup semantics, including OpenSSH hashed hosts.
    No network connection, key scan, trust adoption or private-key read occurs.
    """
    endpoints=gerrit_ssh_endpoints(config)
    try:
        from paramiko import HostKeys
        from paramiko.hostkeys import HostKeyEntry
        payload=read_bounded_bytes(source,max_bytes=4*1024**2).decode('utf-8')
        keys=HostKeys()
        for line in payload.splitlines():
            stripped=line.strip()
            if not stripped or stripped.startswith('#'):continue
            if stripped.startswith('@'):raise ValueError('Host-key marker policy requires separate handling')
            entry=HostKeyEntry.from_line(stripped)
            if entry is None or entry.key is None:raise ValueError('Unrecognized SSH host key')
            for hostname in entry.hostnames:keys.add(hostname,entry.key.get_name(),entry.key)
        if any(not keys.lookup(endpoint) for endpoint in endpoints):raise ValueError('A configured SSH endpoint has no parsed pin')
    except Exception as error:
        # Upstream parsing errors can include private hostnames/key material.
        # Keep the launch refusal fixed and do not fall back to warning/TOFU.
        raise ZuulLaunchError('Provision parseable approved host-key pins for every configured Gerrit SSH endpoint') from error


def verify_gerrit_http_policy(config: Path) -> None:
    """Offline check of the selected driver's boolean TLS policy, without I/O."""
    parser=configparser.RawConfigParser(strict=True)
    try:
        parser.read_string(read_bounded_bytes(config,max_bytes=4*1024**2).decode('utf-8'))
        for section in parser.sections():
            if not section.startswith('connection "') or parser.get(section,'driver',fallback='')!='gerrit':continue
            options=dict(parser.items(section))
            # The reviewed upstream constructor retains allowed INI strings;
            # Requests treats 'true' as a CA filename. Its omitted default is
            # the actual bool True, combined with REQUESTS_CA_BUNDLE.
            if 'verify_ssl' in options:raise ValueError('Require the reviewed boolean TLS default')
            if not options.get('password'):continue
            if options.get('auth_type','basic') not in {'basic','digest','form'}:raise ValueError('Unsupported offline authentication policy')
            from zuul.driver.gerrit import GerritDriver
            connection=GerritDriver().getConnection('offline-tls-policy',options)
            try:
                if connection.verify_ssl is not True:raise ValueError('Gerrit HTTP verification is not boolean True')
            finally:connection.session.close()
    except Exception as error:
        raise ZuulLaunchError('The selected Gerrit HTTP driver must retain certificate verification with the approved CA bundle') from error


def runtime_python(binary: str) -> str:
    """Require the selected console script's explicit application interpreter."""
    lines=read_bounded_bytes(binary,max_bytes=65536).splitlines()
    first=lines[0].decode('utf-8') if lines else ''
    interpreter=first[2:] if first.startswith('#!') else ''
    selected=Path(interpreter)
    if (not selected.is_absolute() or not re.fullmatch(r'python(?:3(?:[.][0-9]+)?)?',selected.name)
            or not selected.is_file()):
        raise ZuulLaunchError('Reviewed Zuul CLI must declare its explicit Python interpreter')
    return interpreter


def runtime_check(binary: str, code: str, arguments: list[str], refusal: str) -> None:
    result=run_bounded([runtime_python(binary),'-I','-B','-c',code,str(Path(__file__).resolve().parent),*arguments],
        timeout=20,text=True,input='',output_max_bytes=8192,check=False,
        env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
    if result.returncode:raise ZuulLaunchError(refusal)


def verify_runtime_gerrit_host_pins(binary: str, config: Path, source: Path) -> None:
    """Use the selected daemon's interpreter, including private virtualenvs."""
    code=("import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
          "import start_zuul;start_zuul.verify_gerrit_host_pins(Path(sys.argv[2]),Path(sys.argv[3]));"
          "start_zuul.verify_gerrit_http_policy(Path(sys.argv[2]))")
    runtime_check(binary,code,[str(config),str(source)],
        'The selected Zuul runtime did not verify every configured Gerrit SSH host pin')


# Version labels alone do not identify downstream policy changes. Every accepted
# source must have its own reviewed artifact/image digest in private provisioning.
ZOOKEEPER_TLS_CLIENTS={
    '14.3.0': 'ef2ad74fce17a87424dbb8fd9c25ac22dad5bc4e787806ae36ec9a849a81d32c',
}


def verify_zookeeper_tls_config(config_root: Path) -> None:
    parser=configparser.RawConfigParser(strict=True)
    try:
        parser.read_string(read_bounded_bytes(config_root/'zuul.conf',max_bytes=4*1024**2).decode('utf-8'))
        for setting,filename in (('tls_cert','zk-client.pem'),('tls_key','zk-client.key'),('tls_ca','zk-ca.pem')):
            if parser.get('zookeeper',setting,fallback='')!=str(config_root/'private'/filename):
                raise ZuulLaunchError('ZooKeeper requires explicit provisioned TLS certificate, key and CA paths')
    except (configparser.Error,UnicodeError) as error:
        raise ZuulLaunchError('ZooKeeper TLS configuration is invalid') from error


def verify_zookeeper_client_policy(expected: str) -> None:
    """Offline check of the selected interpreter's reviewed immutable client."""
    digest=ZOOKEEPER_TLS_CLIENTS.get(expected)
    if digest is None:raise ZuulLaunchError('This Zuul release has no reviewed ZooKeeper TLS client policy')
    import zuul.zk
    source=Path(zuul.zk.__file__)
    if (importlib.metadata.version('zuul')!=expected or source.suffix!='.py'
            or hashlib.sha256(read_bounded_bytes(source,max_bytes=1024**2)).hexdigest()!=digest):
        raise ZuulLaunchError('Install the separately reviewed strict-TLS Zuul candidate; upstream version alone is insufficient')


def verify_runtime_zookeeper_tls(binary: str, config_root: Path, expected: str) -> None:
    verify_zookeeper_tls_config(config_root)
    code=("import sys;sys.path.insert(0,sys.argv[1]);"
          "import start_zuul;start_zuul.verify_zookeeper_client_policy(sys.argv[2])")
    runtime_check(binary,code,[expected],
        'The selected Zuul runtime did not verify the reviewed ZooKeeper TLS client policy')


def executor_checks(config: Path, state: Path, daemon: str) -> None:
    if sys.platform != "linux" or os.geteuid() == 0:
        raise ZuulLaunchError("Executor requires a dedicated non-root Linux service identity")
    bubblewrap = shutil.which("bwrap")
    manager = shutil.which("zuul-manage-ansible")
    if not bubblewrap or not manager:
        raise ZuulLaunchError("Executor sandbox and preinstalled Ansible tooling are required")
    interpreter = runtime_python(manager)
    if Path(interpreter) != Path(runtime_python(daemon)):
        raise ZuulLaunchError("Executor and Ansible manager must use the same reviewed interpreter")
    # Zuul 14.3's CLI calls validate() but does not propagate its False
    # result as a nonzero exit status. Check the result in the manager's own
    # interpreter instead, with no inherited package or pip overrides.
    check = ("import sys;from zuul.lib.ansible import AnsibleManager;"
             "sys.exit(0 if AnsibleManager().validate() is True else 1)")
    validation = run_bounded([interpreter, "-I", "-B", "-c", check],
                             timeout=120, text=True, input="", output_max_bytes=65536, check=False,
                             env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"})
    if validation.returncode:
        raise ZuulLaunchError("Preinstalled executor Ansible validation failed")
    with tempfile.TemporaryDirectory(prefix="zuul-sandbox-probe-", dir=state) as directory:
        root = Path(directory)
        atomic_write_text(root / "outside-canary", "fixture-only", replace=False)
        visible = root / "readonly"
        visible.mkdir()
        atomic_write_text(visible / "visible", "fixture-only", replace=False)
        arguments = [bubblewrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
        for system_path in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(system_path).exists():
                arguments += ["--ro-bind", system_path, system_path]
        arguments += ["--ro-bind", str(visible), "/probe", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                      "--setenv", "PATH", "/usr/bin:/bin", "--chdir", "/", "--", "/bin/sh", "-c",
                      ('test -r /probe/visible && test ! -e "$1" && '
                      'net_id=$(readlink /proc/self/ns/net) && test -n "$net_id" && test "$net_id" != "$2" && '
                      'if printf blocked > /probe/created 2>/dev/null; then exit 42; fi'),
                      "zuul-sandbox-probe", str(root / "outside-canary"), os.readlink("/proc/self/ns/net")]
        sandbox = run_bounded(arguments, timeout=20, text=True, input="", output_max_bytes=4096, check=False)
        if sandbox.returncode or (visible / "created").exists():
            raise ZuulLaunchError("Executor filesystem/network namespace probe failed; no privilege fallback is allowed")


def launch(role: str, expected_version: str, *, config_root: Path = Path("/etc/zuul"), state: Path = Path("/var/lib/zuul")) -> None:
    if role not in ROLES:
        raise ZuulLaunchError("Unknown role")
    if role == "launcher" and expected_version != "14.3.0":
        raise ZuulLaunchError("Integrated launcher requires the reviewed Zuul version")
    if sys.platform != "linux" or os.geteuid() == 0:
        raise ZuulLaunchError("Zuul launch requires a dedicated non-root Linux identity")
    if os.environ.get('GIT_SSL_NO_VERIFY','').casefold() not in {'','false','0','no','off'} or os.environ.get('GIT_CONFIG_PARAMETERS') or os.environ.get('GIT_CONFIG_COUNT'):
        raise ZuulLaunchError('Provision Git TLS through the guarded configuration rather than inherited verification overrides')
    verify_inputs(dict(os.environ), role, config_root=config_root)
    binary = shutil.which(f"zuul-{role}")
    if not binary:
        raise ZuulLaunchError("Reviewed Zuul daemon is not installed")
    check_version(binary, expected_version)
    verify_runtime_zookeeper_tls(binary, config_root, expected_version)
    if not state.is_dir() or state.is_symlink():
        raise ZuulLaunchError("Provision the dedicated writable service state directory first")
    verify_runtime_gerrit_host_pins(binary, config_root / "zuul.conf", config_root / "private/known_hosts")
    if role == "executor":
        executor_checks(config_root / "zuul.conf", state, binary)
    stage_known_hosts(config_root / "private/known_hosts", state)
    verify_runtime_gerrit_host_pins(binary, config_root / "zuul.conf", state / ".ssh/known_hosts")
    os.environ["HOME"] = str(state)
    os.environ['GIT_SSL_CAINFO']=str(config_root/'private/gerrit-ca.pem')
    os.environ['GIT_CONFIG_COUNT']='2'
    os.environ['GIT_CONFIG_KEY_0']='http.sslVerify';os.environ['GIT_CONFIG_VALUE_0']='true'
    os.environ['GIT_CONFIG_KEY_1']='http.sslCAInfo';os.environ['GIT_CONFIG_VALUE_1']=str(config_root/'private/gerrit-ca.pem')
    os.environ["GIT_SSH_COMMAND"] = shlex.join([
        "ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={config_root}/private/known_hosts", "-i", str(config_root / "private/gerrit-ssh.key")])
    # Upstream readConfig uses ConfigParser(defaults=ZUUL_environment). Escape
    # literal percent signs once at this boundary, including URI percent-encoding,
    # so interpolation neither rejects credentials nor expands embedded references.
    for name in ('ZUUL_DATABASE_URI','ZUUL_KEYSTORE_PASSWORD','ZUUL_GERRIT_HTTP_PASSWORD'):
        os.environ[name]=os.environ[name].replace('%','%%')
    # Fixed program/argument vectors, never a shell command assembled from secrets.
    os.execv(binary, [binary, "-f", "-c", str(config_root / "zuul.conf")])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--expected-version")
    args = parser.parse_args(argv)
    try:
        if args.start:
            if not args.expected_version:
                raise ZuulLaunchError("Explicit launch requires a reviewed daemon version")
            launch(args.role, args.expected_version)
            raise ZuulLaunchError("Daemon exec returned unexpectedly")
        if args.expected_version:
            raise ZuulLaunchError("Version selection is only accepted for explicit launch")
        print(json.dumps(verify_inputs(dict(os.environ), args.role)))
        return 0
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        print("Zuul launch/input check stopped; review private provisioning, version and sandbox prerequisites. No fallback was used.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
