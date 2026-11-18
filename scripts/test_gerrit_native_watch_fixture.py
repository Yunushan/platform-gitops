#!/usr/bin/env python3
"""Real transient services for the reviewed native Gerrit watch packages."""

import configparser
import copy
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from kubernetes_management import KubernetesOperationError
from strict_json import loads_strict_json
from test_gerrit_native_promotion_fixture import process_identity


def failure_records(payload):
    """Retain only fixed safe reporter diagnostics before private-log cleanup."""
    records = []
    signals = set(KubernetesOperationError(0, "").diagnostic["signals"])
    for line in payload.decode(errors="replace").splitlines():
        try: value = json.loads(line)
        except ValueError: continue
        value = value.get("gerrit_reporter_failure") if isinstance(value, dict) else None
        if not isinstance(value, dict): continue
        kind = value.get("exception_type")
        if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z_]{1,80}", kind): continue
        locations = value.get("code_locations", [])
        record = {"exception_type": kind, "code_locations": [item for item in locations
            if isinstance(item, str) and re.fullmatch(r"[a-z_]+[.]py:[0-9]{1,6} [A-Za-z_<>]+", item)][-8:]} if isinstance(locations, list) else {"exception_type": kind}
        detail = value.get("kubernetes")
        if isinstance(detail, dict) and type(detail.get("return_code")) is int and isinstance(detail.get("signals"), dict):
            record["kubernetes"] = {"return_code": detail["return_code"], "signals": {
                name: flag for name, flag in detail["signals"].items() if name in signals and type(flag) is bool}}
        records.append(record)
    return records[-8:]


def command(arguments, *, check=True):
    return run_bounded(arguments, text=True, timeout=30, output_max_bytes=256 * 1024, check=check)


def service_settings(unit, base, digest, account):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(unit)
    selected = dict(parser["Service"])
    expected = {"type": "simple", "user": account, "group": account, "umask": "0077", "restart": "on-failure", "restartsec": "10",
        "nonewprivileges": "yes", "protectsystem": "strict", "protecthome": "yes", "privatetmp": "yes", "privatedevices": "yes",
        "restrictsuidsgid": "yes", "capabilityboundingset": "", "restrictaddressfamilies": "AF_INET AF_INET6 AF_UNIX", "memorymax": "512M", "tasksmax": "64"}
    if set(selected) != {*expected, "environment", "execstart"} or any(selected[k] != v for k, v in expected.items()):
        raise ValueError("Reviewed native service settings changed")
    arguments = shlex.split(selected["execstart"])
    program = "tekton_gerrit_reporter.py" if account.endswith("reporter") else "tekton_gerrit_bridge.py"
    if arguments != ["/usr/bin/python3", "-B", base + "/" + program, "--bundle-dir", base, "--contract-sha256", digest, "--watch"]:
        raise ValueError("Require the original reviewed native watch command")
    environment = shlex.split(selected["environment"])
    if len(environment) != 1 or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}=/run/platform/gerrit-tekton(?:-reporter)?/kubeconfig", environment[0]):
        raise ValueError("Require the scoped fixed credential reference")
    properties = {k: v for k, v in expected.items() if k not in {"user", "group"}}
    return arguments, environment[0], properties



def watch_runtime_limit(context):
    from test_k3s_vm_fixture import guest_identity_lifetime
    return 1700 if guest_identity_lifetime(context)=='30m' else 300


def owner_unit_name(cgroup):
    match=re.fullmatch(r'0::(/system[.]slice/(platform-k3s-vm-[a-z0-9-]+[.]service))\n?',cgroup)
    if not match:raise ValueError('Extended watches require their original bounded K3s parent unit')
    return match.group(2),match.group(1)


def runtime_limit_microseconds(value):
    if not isinstance(value,str) or not re.fullmatch(r'(?:[0-9]+(?:us|ms|min|h|s)\s*)+',value):
        raise ValueError('Require a finite observed watch-service lifetime')
    units={'us':1,'ms':1000,'s':1000000,'min':60000000,'h':3600000000}
    return sum(int(number)*units[unit] for number,unit in re.findall(r'([0-9]+)(us|ms|min|h|s)',value))


def poll_log_records(payload):
    rows=[]
    for line in payload.decode().splitlines():
        try:row=json.loads(line)
        except ValueError:continue
        if isinstance(row,dict):rows.append(row)
    return rows


def successful_poll_rows(kind,rows,after):
    from tekton_gerrit_bridge import FALSE_FLAGS
    counters={'bridge':{'checked','accepted','duplicates'},'reporter':{'pending','reported','already-reported','rejected'}}
    if kind not in counters or type(after) is not int or after<0 or not isinstance(rows,list):raise ValueError('Require an original watch poll offset')
    fields=counters[kind];expected=fields|set(FALSE_FLAGS)
    return [{'index':index,'summary':copy.deepcopy(row)} for index,row in enumerate(rows) if index>=after and isinstance(row,dict)
        and set(row)==expected and all(type(row[k]) is int and row[k]>=0 for k in fields) and all(row[k] is False for k in FALSE_FLAGS)]


def fresh_poll_contract(kind,bundle,digest):
    files={'bridge':'gerrit-event-contract.json','reporter':'gerrit-report-contract.json'}
    if kind not in files:raise ValueError('Require an original watch role')
    payload=read_bounded_bytes(bundle/files[kind],max_bytes=128*1024)
    if not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest) or hashlib.sha256(payload).hexdigest()!=digest:
        raise ValueError('Original watch poll contract changed')
    value=loads_strict_json(payload);interval=value['plan']['poll_seconds']
    if type(interval) is not int or not 10<=interval<=300:raise ValueError('Require a finite validated watch polling interval')
    return {'contract_text':payload.decode(),'contract_sha256':digest,'poll_seconds':interval,'wait_budget_seconds':3*interval,'required_records':2}


def original_process_absent(original, proc_root=Path("/proc")):
    """PID reuse and a later boot cannot keep an original invocation alive."""
    boot = (proc_root / "sys/kernel/random/boot_id").read_text().strip()
    if boot != original["boot_id"]:
        return True
    try:
        ticks = int((proc_root / str(original["pid"]) / "stat").read_text().rsplit(") ", 1)[1].split()[19])
    except (FileNotFoundError, ProcessLookupError):
        return True
    return ticks != original["start_ticks"]

class WatchPair:
    def __init__(self, test, context, root, bundles, inputs, digests, units):
        self.test, self.context, self.root = test, context, root
        self.bundles, self.inputs, self.digests, self.units = bundles, inputs, digests, units
        self.handles = {}
        self.runtime_limit=watch_runtime_limit(context)
        self.owner=None
        if self.runtime_limit==1700:
            unit,cgroup=owner_unit_name(Path('/proc/self/cgroup').read_text())
            info=self.show(unit)
            if info.get('ActiveState')!='active' or info.get('ControlGroup')!=cgroup or not re.fullmatch(r'[0-9a-f]{32}',info.get('InvocationID','')):
                raise ValueError('Original bounded watch owner is not active')
            self.owner={'unit':unit,'control_group':cgroup,'invocation_id':info['InvocationID'],'main_pid':info['MainPID']}

        self.network = Path("/proc/self/ns/net").stat().st_ino
        self.owner_pid = os.getpid()
        if os.geteuid() != 0 or root.is_symlink() or root.parent != Path("/var/tmp") or not root.name.startswith("platform-gerrit-review-"):
            raise ValueError("Use the original private Gerrit worker scope")

    def show(self, name):
        value = command(["systemctl", "show", "--all", name], check=False)
        return dict(line.split("=", 1) for line in value.stdout.splitlines() if "=" in line)

    def start(self, kind):
        base = "/opt/platform/gerrit-tekton" + ("-reporter" if kind == "reporter" else "")
        account = "platform-ci-reporter" if kind == "reporter" else "platform-ci-bridge"
        args, environment, properties = service_settings(self.units[kind], base, self.digests[kind], account)
        for directory in (self.bundles[kind], self.inputs[kind]):
            if directory.parent != self.root or directory.is_symlink(): raise ValueError("Require original private package/input directories")
            for path in (directory, *directory.iterdir()):
                if path.is_symlink() or path != directory and not path.is_file(): raise ValueError("Unexpected native input/package entry")
                os.chown(path, 0, 0)
                path.chmod((0o755 if path.is_dir() else 0o444) if directory == self.bundles[kind] else (0o500 if path.is_dir() else 0o400))
        name = "platform-gerrit-watch-" + kind + "-" + uuid4().hex + ".service"
        user = "p-gw-" + uuid4().hex[:12]
        log = self.root / (kind + "-watch.log")
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.close(descriptor)
        # These laboratory-only mappings retain the rendered hardening and
        # command. Each service gets its own ephemeral identity and mount view;
        # no host account, persistent unit or global /opt or /run file is made.
        credentials = ["LoadCredential=" + name + ":" + str(self.inputs[kind] / name) for name in ("api-token", "ca.crt", "kubeconfig")]
        extra = [*credentials, "DynamicUser=yes", "User=" + user, "Group=" + user,
            "RuntimeMaxSec="+str(self.runtime_limit), "KillMode=control-group", "StartLimitIntervalSec=600", "StartLimitBurst=3",
            "After=network-online.target"+(" "+self.owner["unit"] if self.owner else ""), "Wants=network-online.target",
            "NetworkNamespacePath=/proc/" + str(self.owner_pid) + "/ns/net",
            "TemporaryFileSystem=/opt:ro /run:ro /usr/local/bin:ro",
            "BindReadOnlyPaths=" + str(self.bundles[kind]) + ":" + base + " /run/credentials/" + name + ":/run/platform/gerrit-tekton" + ("-reporter" if kind == "reporter" else "") + " " + self.context["client"].prefix[0] + ":/usr/local/bin/kubectl",
            "StandardOutput=append:" + str(log), "StandardError=inherit"]
        names = {"type": "Type", "umask": "UMask", "restart": "Restart", "restartsec": "RestartSec", "nonewprivileges": "NoNewPrivileges",
            "protectsystem": "ProtectSystem", "protecthome": "ProtectHome", "privatetmp": "PrivateTmp", "privatedevices": "PrivateDevices",
            "restrictsuidsgid": "RestrictSUIDSGID", "capabilityboundingset": "CapabilityBoundingSet", "restrictaddressfamilies": "RestrictAddressFamilies", "memorymax": "MemoryMax", "tasksmax": "TasksMax"}
        options = [names[k] + "=" + v for k, v in properties.items() if k in names]
        if self.owner:extra.append("BindsTo="+self.owner["unit"])
        handle = {"name": name, "uid": None, "user": user, "base": base, "digest": self.digests[kind], "log": log, "fragment": None, "args": args, "processes": []}
        self.handles[kind] = handle
        command(["systemd-run", "--quiet", "--collect", "--unit=" + name, "--description=Owned synthetic Gerrit watch " + self.digests[kind],
            "--setenv=" + environment, *["--property=" + v for v in options + extra], *args])
        def started():
            info = self.show(name)
            if info.get("ActiveState") == "failed" or info.get("LoadState") == "not-found": raise AssertionError("Original native watch service failed")
            if info.get("ActiveState") != "active" or not int(info.get("MainPID", "0")): return None
            original = process_identity(info)
            if original is None: return None
            if Path("/proc/" + info["MainPID"] + "/cmdline").read_bytes().rstrip(b"\0").decode().split("\0") != args: return None
            if len(set(original["uid"])) != 1: return None
            return info, original
        info, original = self.context["wait"](started, seconds=30)
        handle["uid"] = original["uid"][0]
        handle["processes"].append(original)
        fragment = Path(info["FragmentPath"])
        if fragment != Path("/run/systemd/transient") / name or fragment.is_symlink(): raise ValueError("Original transient unit path changed")
        handle["fragment"] = (fragment.stat().st_ino, hashlib.sha256(fragment.read_bytes()).hexdigest())
        handle["invocation"] = info["InvocationID"]
        self.inspect(kind)

    def inspect(self, kind):
        handle = self.handles[kind]; info = self.show(handle["name"])
        if info.get("ActiveState") != "active" or info.get("SubState") != "running": raise AssertionError("Original watch service is not running")
        self.test.assertEqual(info.get("Transient"), "yes")
        self.test.assertEqual(info.get("User"), handle["user"])
        self.test.assertEqual(info.get("Group"), handle["user"])
        self.test.assertEqual(info.get("DynamicUser"), "yes")
        self.test.assertEqual(runtime_limit_microseconds(info.get("RuntimeMaxUSec")),self.runtime_limit*1000000)
        if self.owner:
            self.test.assertEqual(set(info.get('BindsTo','').split()),{self.owner['unit']})
            self.test.assertIn(self.owner['unit'],info.get('After','').split())
            owner=self.show(self.owner['unit'])
            self.test.assertEqual((owner.get('InvocationID'),owner.get('ControlGroup'),owner.get('MainPID'),owner.get('ActiveState')),
                (self.owner['invocation_id'],self.owner['control_group'],self.owner['main_pid'],'active'))

        self.test.assertEqual(info.get("InvocationID"), handle["invocation"])
        observed = process_identity(info)
        self.test.assertIsNotNone(observed)
        original = handle["processes"][-1]
        for key in ("pid", "start_ticks", "boot_id", "invocation_id", "control_group", "uid"):
            self.test.assertEqual(observed[key], original[key])
        self.test.assertIs(observed["python_executable_matches"], True)
        self.test.assertIs(observed["network_namespace_matches"], True)
        self.test.assertEqual(info.get("MemoryMax"), str(512 * 1024 ** 2)); self.test.assertEqual(info.get("TasksMax"), "64")
        for key, expected in (("NoNewPrivileges", "yes"), ("ProtectSystem", "strict"), ("ProtectHome", "yes"), ("PrivateTmp", "yes"), ("PrivateDevices", "yes"), ("CapabilityBoundingSet", "")):
            self.test.assertEqual(info.get(key), expected)
        pid = int(info["MainPID"])
        self.test.assertEqual(Path("/proc/" + str(pid) + "/cmdline").read_bytes().rstrip(b"\0").decode().split("\0"), handle["args"])
        self.test.assertEqual(Path("/proc/" + str(pid) + "/ns/net").stat().st_ino, self.network)
        status = dict(line.split(":", 1) for line in Path("/proc/" + str(pid) + "/status").read_text().splitlines() if ":" in line)
        self.test.assertEqual(set(status["Uid"].split()), {str(handle["uid"])})
        self.test.assertEqual(status["NoNewPrivs"].strip(), "1"); self.test.assertEqual(int(status["CapEff"].strip(), 16), 0)
        mounts = [line.split() for line in Path("/proc/" + str(pid) + "/mountinfo").read_text().splitlines()]
        for target in (handle["base"], "/run/platform/gerrit-tekton" + ("-reporter" if kind == "reporter" else "")):
            rows = [row for row in mounts if row[4] == target]
            self.test.assertEqual(len(rows), 1); self.test.assertIn("ro", rows[0][5].split(","))
        if handle["fragment"]:
            fragment = Path(info["FragmentPath"])
            self.test.assertEqual((fragment.stat().st_ino, hashlib.sha256(fragment.read_bytes()).hexdigest()), handle["fragment"])
        return info

    def snapshot(self):
        result={'monotonic':time.monotonic(),'owner':copy.deepcopy(self.owner),'watches':{}}
        for kind,handle in self.handles.items():
            info=self.inspect(kind);rows=self.reports(kind);log=handle['log'].stat()
            result['watches'][kind]={'unit':handle['name'],'invocation_id':info['InvocationID'],'process':copy.deepcopy(handle['processes'][-1]),
                'restart_count':int(info['NRestarts']),'runtime_limit_seconds':self.runtime_limit,'runtime_max_usec':runtime_limit_microseconds(info['RuntimeMaxUSec']),
                'poll_rows':len(rows),'log_identity':{'device':log.st_dev,'inode':log.st_ino}}
        return result

    def fresh_poll(self,kind):
        profile=fresh_poll_contract(kind,self.bundles[kind],self.handles[kind]['digest'])
        fence=time.monotonic();deadline=fence+profile['wait_budget_seconds'];offset=len(self.reports(kind))
        observation={**profile,'offset':offset,'fence_monotonic':fence,'deadline_monotonic':deadline,'records':[],'completed':False}
        if not hasattr(self,'fresh_poll_observations'):self.fresh_poll_observations={}
        self.fresh_poll_observations[kind]=observation
        def observed():
            rows=self.reports(kind);matched=successful_poll_rows(kind,rows,offset);now=time.monotonic()
            observation.update(records=matched,observed_monotonic=now,total_rows=len(rows))
            if now>deadline:raise TimeoutError('Late watch polls cannot qualify original recovery')
            return len(matched)>=profile['required_records']
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('Original watch poll budget exhausted')
        self.context['wait'](observed,seconds=remaining)
        observation['completed']=True
        return copy.deepcopy(observation)

    def reports(self, kind):
        self.inspect(kind)
        return poll_log_records(read_bounded_bytes(self.handles[kind]["log"], max_bytes=256 * 1024))

    def restart(self, kind):
        before = self.inspect(kind)
        command(["systemctl", "kill", "--kill-whom=main", "--signal=KILL", self.handles[kind]["name"]])
        def restarted():
            info = self.show(self.handles[kind]["name"])
            if info.get("LoadState") == "not-found" or info.get("ActiveState") == "failed": raise AssertionError("Native watch restart failed")
            if info.get("ActiveState") != "active" or info.get("SubState") != "running" or info.get("InvocationID") == before["InvocationID"]: return None
            self.test.assertNotEqual(info["MainPID"], before["MainPID"])
            self.test.assertEqual(int(info["NRestarts"]), int(before["NRestarts"]) + 1)
            original = process_identity(info)
            if original is None or len(set(original["uid"])) != 1: return None
            if Path("/proc/" + str(original["pid"]) + "/cmdline").read_bytes().rstrip(b"\0").decode().split("\0") != self.handles[kind]["args"]: return None
            previous = self.handles[kind]["processes"][-1]
            self.test.assertTrue(original_process_absent(previous))
            previous["original_process_removed_before_restart"] = True
            self.handles[kind]["uid"] = original["uid"][0]
            self.handles[kind]["invocation"] = info["InvocationID"]
            self.handles[kind]["processes"].append(original)
            return self.inspect(kind)
        self.context["wait"](restarted, seconds=30)

    def wait_report(self, kind, field, *, after=0):
        return self.context["wait"](lambda: any(type(row.get(field)) is int and row[field] > 0 for row in self.reports(kind)[after:]), seconds=45)

    def uids(self):
        return {v["uid"][0] for handle in self.handles.values() for v in handle["processes"]}

    def audit(self, tokens):
        private_values = [token.encode() for token in tokens]
        for kind in self.handles:
            private_values.append(read_bounded_bytes(self.inputs[kind] / "api-token").strip())
            config = json.loads(read_bounded_bytes(self.inputs[kind] / "kubeconfig"))
            private_values.extend(item["user"]["token"].encode() for item in config["users"])
        result = {}
        for kind, handle in self.handles.items():
            self.inspect(kind)
            payload = read_bounded_bytes(handle["log"], max_bytes=256 * 1024)
            self.test.assertFalse(any(value in payload for value in private_values))
            fields = ("accepted", "duplicates", "reported", "already-reported", "pending", "rejected")
            rows = poll_log_records(payload)
            totals = {key: sum(row.get(key, 0) for row in rows if type(row.get(key)) is int) for key in fields}
            result[kind] = {"unit": handle["name"], "uid": handle["uid"], "invocation_id": handle["invocation"],
                "contract_sha256": handle["digest"], "command": handle["args"], "unit_fragment_sha256": handle["fragment"][1],
                "restarts": int(self.show(handle["name"])["NRestarts"]), "processes": handle["processes"], "report_totals": totals,
                "runtime_limit_seconds":self.runtime_limit,"runtime_max_usec":runtime_limit_microseconds(self.show(handle["name"])["RuntimeMaxUSec"]),
                "poll_log":payload.decode(),"poll_log_sha256":hashlib.sha256(payload).hexdigest(),"owner":copy.deepcopy(self.owner)}
        self.test.assertNotEqual(self.handles["bridge"]["uid"], self.handles["reporter"]["uid"])
        return result

    def __enter__(self):
        try:
            self.start("bridge"); self.start("reporter")
            self.test.assertNotEqual(self.handles["bridge"]["uid"], self.handles["reporter"]["uid"])
            return self
        except Exception:
            print(json.dumps({"native_watch_start_failed": True, "service_states": [
                {k: self.show(handle["name"]).get(k) for k in ("LoadState", "ActiveState", "Result", "ExecMainStatus", "NRestarts")}
                for handle in self.handles.values()]}), flush=True)
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *error):
        if error and error[0] is not None:
            for kind, handle in self.handles.items():
                try:
                    info = self.show(handle["name"])
                    print(json.dumps({"native_watch_failure": {"kind": kind,
                        "invocation_changed": info.get("InvocationID") != handle.get("invocation"),
                        "restart_count": int(info.get("NRestarts", "0")),
                        "failures": failure_records(read_bounded_bytes(handle["log"], max_bytes=256 * 1024))}}), flush=True)
                except (OSError,ValueError,KeyError,subprocess.SubprocessError):
                    print(json.dumps({"native_watch_failure": {"kind": kind, "diagnostic_capture_failed": True}}), flush=True)
        for handle in reversed(list(self.handles.values())):
            info = self.show(handle["name"])
            if info.get("LoadState") == "not-found":
                for original in handle["processes"]:
                    self.test.assertTrue(original_process_absent(original))
                    self.test.assertFalse(Path("/sys/fs/cgroup" + original["control_group"]).exists())
                    original["original_process_and_cgroup_removed"] = True
                continue
            if info.get("Description") != "Owned synthetic Gerrit watch " + handle["digest"] or info.get("Transient") != "yes":
                raise ValueError("Refuse to stop a replaced native unit")
            if handle["fragment"]:
                fragment = Path(info["FragmentPath"])
                if (fragment.stat().st_ino, hashlib.sha256(fragment.read_bytes()).hexdigest()) != handle["fragment"]: raise ValueError("Original native unit file changed")
            command(["systemctl", "stop", handle["name"]])
            def stopped(handle=handle):
                state = self.show(handle["name"])
                return state.get("LoadState") == "not-found" or state.get("ActiveState") == "inactive" and state.get("MainPID") == "0"
            self.context["wait"](stopped, seconds=30)
            for original in handle["processes"]:
                self.context["wait"](lambda original=original: original_process_absent(original)
                    and not Path("/sys/fs/cgroup" + original["control_group"]).exists(), seconds=30)
                original["original_process_and_cgroup_removed"] = True
        return False
