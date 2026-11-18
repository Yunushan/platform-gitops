#!/usr/bin/env python3
"""Validate prepared Patroni inputs or explicitly start one reviewed member."""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys

from atomic_file import atomic_write_text
from bounded_file import read_bootstrap_bytes
from bounded_subprocess import run_bounded
import materialize_patroni as materializer
from postgres_ha_policy import STRICT_PATRONI_DCS, STRICT_PATRONI_KAZOO


WATCHDOG = Path("/dev/watchdog")
PATRONI_IDENTITY = """\
from hashlib import sha256
from importlib.util import find_spec
from kazoo import __version__ as kazoo_version
from patroni.version import __version__ as patroni_version

with open(find_spec('patroni.dcs.zookeeper').origin, 'rb') as source:
    print(patroni_version)
    print(kazoo_version)
    print(sha256(source.read(256 * 1024 + 1)).hexdigest())
"""


def check_executables(plan, config, environment, *, validate_configuration):
    postgres = Path(plan["native"]["postgres_bin_dir"]) / "postgres"
    python = [sys.executable, "-I", "-B"]
    command = [*python, "-m", "patroni"]
    release = plan["patroni_version"]
    reviewed = STRICT_PATRONI_DCS.get(release)
    if reviewed is None:
        raise ValueError("Require a reviewed Patroni release with ZooKeeper hostname verification")
    expected_identity = "\n".join((release, STRICT_PATRONI_KAZOO, reviewed))
    checks = [
        ([str(postgres), "--version"], re.escape("postgres (PostgreSQL) " + plan["postgres_version"]) + r"(?: \([^\r\n]+\))?"),
        ([*python, "-c", PATRONI_IDENTITY], re.escape(expected_identity)),
    ]
    # Patroni's optional validator probes every ZooKeeper endpoint. Startup
    # must leave quorum and peer reachability decisions to the daemon.
    if validate_configuration:
        checks.append(([*command, "--validate-config", str(config)], None))
    for argv, version in checks:
        result = run_bounded(argv, text=True, timeout=30, output_max_bytes=128 * 1024,
                             env=environment, input="", check=False, cwd=config.parent)
        if result.returncode or (version is not None and not re.fullmatch(version, result.stdout.strip())):
            raise ValueError("Installed database tools or private configuration failed validation")
    return [*command, str(config)]


def claim_state(plan, member):
    import fcntl

    state = Path(member["state_path"])
    materializer.directory(state)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    handle = os.open(state, flags)
    try:
        info = os.fstat(handle)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Require an owned private state directory")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        witness = state / ".platform-patroni.json"
        content = json.dumps({"schema_version": 1, "cluster_id": plan["cluster_id"], "member": member["name"],
                              "state_path": member["state_path"]}, sort_keys=True) + "\n"
        names = set(os.listdir(handle))
        if witness.name in names:
            materializer.regular(witness, private=True)
            if read_bootstrap_bytes(witness, 4096, follow_symlinks=False) != content.encode() or names - {witness.name, "pgdata"}:
                raise ValueError("Database state identity differs from the reviewed member")
            if "pgdata" in names:
                materializer.directory(state / "pgdata")
        else:
            if names:
                raise ValueError("Start with an empty dedicated state directory")
            atomic_write_text(witness, content, replace=False)
        return handle
    except BaseException:
        os.close(handle)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--member", required=True)
    parser.add_argument("--inputs-dir", type=Path, default=Path("/etc/platform-postgres/inputs"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-runtime", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        bundle = Path(__file__).resolve().parent
        contract, _ = materializer.load(bundle, args.contract_sha256, args.member)
        if "start_patroni.py" not in contract["files_sha256"]:
            raise ValueError("Require a reviewed host startup package")
        result = {"local_package_verified": True, "runtime_configuration_verified": False, "full_stack_verified": False}
        if args.check_runtime or args.execute:
            plan = contract["plan"]
            member = next(item for item in plan["members"] if item["name"] == args.member)
            runtime = Path("/run/platform-postgres-" + args.member)
            config = runtime / "patroni.yaml"
            materializer.operate(bundle, args.contract_sha256, args.member, materialize=True,
                                 inputs=args.inputs_dir, output=config, managed_state=True)
            environment = {"PATH": "/usr/bin:/bin", "HOME": str(runtime), "TMPDIR": str(runtime), "LC_ALL": "C"}
            command = check_executables(plan, config, environment, validate_configuration=args.check_runtime)
            result["runtime_configuration_verified"] = args.check_runtime
            if args.execute:
                if not stat.S_ISCHR(WATCHDOG.stat().st_mode) or not os.access(WATCHDOG, os.R_OK | os.W_OK):
                    raise ValueError("Require the accessible dedicated watchdog device")
                handle = claim_state(plan, member)
                try:
                    os.chdir(runtime)
                    os.set_inheritable(handle, True)
                    os.execve(command[0], command, environment)
                finally:
                    os.close(handle)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print("Patroni startup refused; private inputs and command output were suppressed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
